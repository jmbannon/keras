"""Multi-GPU training utilities.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from ..layers.merge import concatenate
from .. import backend as K
from ..layers.core import Lambda
from ..engine.training import Model
from ..models import clone_model
from ..utils.generic_utils import to_list


def _get_available_devices():
    return [x.name for x in K.get_session().list_devices()]


def _normalize_device_name(name):
    name = '/' + ':'.join(name.lower().replace('/', '').split(':')[-2:])
    return name


def multi_gpu_model(model, gpus=None, cpu_merge=True, cpu_relocation=False):
    """Replicates a model on different GPUs.

    Specifically, this function implements single-machine
    multi-GPU data parallelism. It works in the following way:

    - Divide the model's input(s) into multiple sub-batches.
    - Apply a model copy on each sub-batch. Every model copy
        is executed on a dedicated GPU.
    - Concatenate the results (on CPU) into one big batch.

    E.g. if your `batch_size` is 64 and you use `gpus=2`,
    then we will divide the input into 2 sub-batches of 32 samples,
    process each sub-batch on one GPU, then return the full
    batch of 64 processed samples.

    This induces quasi-linear speedup on up to 8 GPUs.

    This function is only available with the TensorFlow backend
    for the time being.

    # Arguments
        model: A Keras model instance. To avoid OOM errors,
            this model could have been built on CPU, for instance
            (see usage example below).
        gpus: Integer >= 2 or list of integers, number of GPUs or
            list of GPU IDs on which to create model replicas.
        cpu_merge: A boolean value to identify whether to force
            merging model weights under the scope of the CPU or not.
        cpu_relocation: A boolean value to identify whether to
            create the model's weights under the scope of the CPU.
            If the model is not defined under any preceding device
            scope, you can still rescue it by activating this option.

    # Returns
        A Keras `Model` instance which can be used just like the initial
        `model` argument, but which distributes its workload on multiple GPUs.

    # Examples

    Example 1 - Training models with weights merge on CPU

    ```python
        import tensorflow as tf
        from keras.applications import Xception
        from keras.utils import multi_gpu_model
        import numpy as np

        num_samples = 1000
        height = 224
        width = 224
        num_classes = 1000

        # Instantiate the base model (or "template" model).
        # We recommend doing this with under a CPU device scope,
        # so that the model's weights are hosted on CPU memory.
        # Otherwise they may end up hosted on a GPU, which would
        # complicate weight sharing.
        with tf.device('/cpu:0'):
            model = Xception(weights=None,
                             input_shape=(height, width, 3),
                             classes=num_classes)

        # Replicates the model on 8 GPUs.
        # This assumes that your machine has 8 available GPUs.
        parallel_model = multi_gpu_model(model, gpus=8)
        parallel_model.compile(loss='categorical_crossentropy',
                               optimizer='rmsprop')

        # Generate dummy data.
        x = np.random.random((num_samples, height, width, 3))
        y = np.random.random((num_samples, num_classes))

        # This `fit` call will be distributed on 8 GPUs.
        # Since the batch size is 256, each GPU will process 32 samples.
        parallel_model.fit(x, y, epochs=20, batch_size=256)

        # Save model via the template model (which shares the same weights):
        model.save('my_model.h5')
    ```

    Example 2 - Training models with weights merge on CPU using cpu_relocation

    ```python
         ..
         # Not needed to change the device scope for model definition:
         model = Xception(weights=None, ..)

         try:
             parallel_model = multi_gpu_model(model, cpu_relocation=True)
             print("Training using multiple GPUs..")
         except ValueError:
             parallel_model = model
             print("Training using single GPU or CPU..")
         parallel_model.compile(..)
         ..
    ```

    Example 3 - Training models with weights merge on GPU (recommended for NV-link)

    ```python
         ..
         # Not needed to change the device scope for model definition:
         model = Xception(weights=None, ..)

         try:
             parallel_model = multi_gpu_model(model, cpu_merge=False)
             print("Training using multiple GPUs..")
         except:
             parallel_model = model
             print("Training using single GPU or CPU..")

         parallel_model.compile(..)
         ..
    ```

    # On model saving

    To save the multi-gpu model, use `.save(fname)` or `.save_weights(fname)`
    with the template model (the argument you passed to `multi_gpu_model`),
    rather than the model returned by `multi_gpu_model`.
    """
    if K.backend() != 'tensorflow':
        raise ValueError('`multi_gpu_model` is only available '
                         'with the TensorFlow backend.')

    available_devices = _get_available_devices()
    available_devices = [_normalize_device_name(name)
                         for name in available_devices]
    if not gpus:
        # Using all visible GPUs when not specifying `gpus`
        # e.g. CUDA_VISIBLE_DEVICES=0,2 python keras_mgpu.py
        gpus = len([x for x in available_devices if '/gpu:' in x])

    if isinstance(gpus, (list, tuple)):
        if len(gpus) <= 1:
            raise ValueError('For multi-gpu usage to be effective, '
                             'call `multi_gpu_model` with `len(gpus) >= 2`. '
                             'Received: `gpus=%s`' % gpus)
        num_gpus = len(gpus)
        target_gpu_ids = gpus
    else:
        if gpus <= 1:
            raise ValueError('For multi-gpu usage to be effective, '
                             'call `multi_gpu_model` with `gpus >= 2`. '
                             'Received: `gpus=%d`' % gpus)
        num_gpus = gpus
        target_gpu_ids = range(num_gpus)

    import tensorflow as tf

    target_devices = ['/cpu:0'] + ['/gpu:%d' % i for i in target_gpu_ids]
    for device in target_devices:
        if device not in available_devices:
            raise ValueError(
                'To call `multi_gpu_model` with `gpus=%s`, '
                'we expect the following devices to be available: %s. '
                'However this machine only has: %s. '
                'Try reducing `gpus`.' % (gpus,
                                          target_devices,
                                          available_devices))

    def get_slice(data, i, parts):
        shape = K.shape(data)
        batch_size = shape[:1]
        input_shape = shape[1:]
        step = batch_size // parts
        if i == parts - 1:
            size = batch_size - step * i
        else:
            size = step
        size = K.concatenate([size, input_shape], axis=0)
        stride = K.concatenate([step, input_shape * 0], axis=0)
        start = stride * i
        if K.is_sparse(data):
            casted_start = tf.cast(start, dtype=tf.int64)
            casted_size = tf.cast(size, dtype=tf.int64)
            return tf.sparse_slice(data, casted_start, casted_size)
        else:
            return K.slice(data, start, size)

    # Relocate the model definition under CPU device scope if needed
    if cpu_relocation:
        with tf.device('/cpu:0'):
            model = clone_model(model)

    all_outputs = []
    for i in range(len(model.outputs)):
        all_outputs.append([])

    # Place a copy of the model on each GPU,
    # each getting a slice of the inputs.
    for i, gpu_id in enumerate(target_gpu_ids):
        with tf.device('/gpu:%d' % gpu_id):
            with tf.name_scope('replica_%d' % gpu_id):
                inputs = []
                # Retrieve a slice of the input.
                for x in model.inputs:
                    if K.is_sparse(x):
                        input_shape = K.int_shape(x)[1:]
                        slice_i = Lambda(get_slice,
                                         output_shape=input_shape,
                                         arguments={'i': i,
                                                    'parts': num_gpus})(x)

                    else:
                        # In-place input splitting which is not only
                        # 5% ~ 12% faster but also less GPU memory
                        # duplication.
                        with tf.device(x.device):
                            input_shape = K.int_shape(x)[1:]
                            slice_i = Lambda(get_slice,
                                             output_shape=input_shape,
                                             arguments={'i': i,
                                                        'parts': num_gpus})(x)
                    inputs.append(slice_i)

                # Apply model on slice
                # (creating a model replica on the target device).
                outputs = model(inputs)
                outputs = to_list(outputs)

                # Save the outputs for merging back together later.
                for o in range(len(outputs)):
                    all_outputs[o].append(outputs[o])

    # Deduplicate output names to handle Siamese networks.
    occurrences = {}
    for n in model.output_names:
        if n not in occurrences:
            occurrences[n] = 1
        else:
            occurrences[n] += 1
    conflict_counter = {n: 0 for n, count in occurrences.items() if count > 1}
    output_names = []
    for n in model.output_names:
        if n in conflict_counter:
            conflict_counter[n] += 1
            n += '_%d' % conflict_counter[n]
        output_names.append(n)

    # Merge outputs under expected scope.
    with tf.device('/cpu:0' if cpu_merge else '/gpu:%d' % target_gpu_ids[0]):
        merged = []
        for name, outputs in zip(output_names, all_outputs):
            merged.append(concatenate(outputs,
                                      axis=0, name=name))
        return Model(model.inputs, merged)
