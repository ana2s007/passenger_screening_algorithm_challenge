from common.caching import read_input_dir, cached, read_log_dir
from common.dataio import get_aps_data_hdf5, get_passenger_clusters, get_data

from . import dataio
from . import tf_models
from . import synthetic_data

import tensorflow as tf
import matplotlib.pyplot as plt
import numpy as np
import skimage.transform
import glob
import os
import tqdm
import h5py
import pickle
import imageio
import math
import time


@cached(get_data, get_aps_data_hdf5, subdir='ssd', version=4)
def get_a3d_projection_data(mode, percentile):
    if not os.path.exists('done'):
        angles, width, height = 16, 512, 660
        tf.reset_default_graph()

        data_in = tf.placeholder(tf.float32, [width//2, width//2, height//2])
        angle = tf.placeholder(tf.float32, [])

        with tf.device('/cpu:0'):
            image = tf.contrib.image.rotate(data_in, -2*math.pi*angle/angles)
        max_proj = tf.reduce_max(image, axis=1)
        mean_proj, var_proj = tf.nn.moments(image, axes=[1])
        std_proj = tf.sqrt(var_proj)

        surf = image > tf.contrib.distributions.percentile(image, percentile, axis=1,
                                                           keep_dims=True)
        dmap = tf.cast(tf.argmax(tf.cast(surf, tf.int32), axis=1) / width, tf.float32)
        proj = tf.image.rot90(tf.stack([dmap, max_proj, mean_proj, std_proj], axis=-1))

        gen = get_data(mode, 'a3d')
        f = h5py.File('data.hdf5', 'w')
        dset = f.create_dataset('dset', (len(gen), angles, height//2, width//2, 5))
        names, labels, dset_in = get_aps_data_hdf5(mode)

        with tf.Session() as sess:
            sess.run(tf.global_variables_initializer())
            for i, (_, _, data) in enumerate(get_data(mode, 'a3d')):
                data = (data[::2,::2,::2]+data[::2,::2,1::2]+data[::2,1::2,::2]+
                        data[::2,1::2,1::2]+data[1::2,::2,::2]+data[1::2,::2,1::2]+
                        data[1::2,1::2,::2]+data[1::2,1::2,1::2])/8
                for j in tqdm.trange(angles):
                    dset[i, j, ..., :-1] = sess.run(proj, feed_dict={data_in: data, angle: j})
                    dset[i, j, ..., -1] = (dset_in[i, ::2, ::2, j]+dset_in[i, ::2, 1::2, j]+
                                           dset_in[i, 1::2, ::2, j]+dset_in[i, 1::2, 1::2, j])


        with open('pkl', 'wb') as f:
            pickle.dump((names, labels), f)
        open('done', 'w').close()
    else:
        with open('pkl', 'rb') as f:
            names, labels = pickle.load(f)
        f = h5py.File('data.hdf5', 'r')
        dset = f['dset']
    return names, labels, dset


@cached(get_a3d_projection_data, subdir='ssd', version=2)
def get_mask_training_data():
    if not os.path.exists('done'):
        names, labels, dset_in = get_a3d_projection_data('sample_large', 97)
        f = h5py.File('data.hdf5', 'w')
        dset = f.create_dataset('dset', (len(dset_in), 330, 256, 6))
        name_idx = {x: i for i, x in enumerate(names)}

        with read_input_dir('hand_labeling/a3d_projections'):
            for file in tqdm.tqdm(glob.glob('*')):
                name, angle = file.replace('.png', '').split('_')
                angle = int(angle)
                angle = 0 if angle == 0 else 16 - angle

                image = imageio.imread(file)
                mask = np.all(image == [255, 0, 0], axis=-1)
                idx = name_idx[name]
                dset[idx, ..., :-1] = dset_in[idx, angle]
                dset[idx, ..., -1] = mask

        with open('pkl', 'wb') as f:
            pickle.dump((names, labels), f)
        open('done', 'w').close()
    else:
        with open('pkl', 'rb') as f:
            names, labels = pickle.load(f)
        f = h5py.File('data.hdf5', 'r')
        dset = f['dset']
    return names, labels, dset


@cached(get_mask_training_data, version=6)
def train_mask_segmentation_cnn(duration, learning_rate=1e-3, model='logistic', min_res=4,
                                num_filters=16):
    assert model in ('logistic', 'hourglass')
    angles, height, width, res, filters = 16, 330, 256, 256, 6

    tf.reset_default_graph()

    data_in = tf.placeholder(tf.float32, [None, height, width, filters])

    # random resize
    size = tf.random_uniform([2], minval=int(0.75*res), maxval=res, dtype=tf.int32)
    h_pad, w_pad = (res-size[0])//2, (res-size[1])//2
    padding = [[0, 0], [h_pad, res-size[0]-h_pad], [w_pad, res-size[1]-w_pad]]
    data = tf.image.resize_images(data_in, size)
    data = tf.stack([tf.pad(data[..., i], padding) for i in range(filters)], axis=-1)

    # random left-right flip
    flip_lr = tf.random_uniform([], maxval=2, dtype=tf.int32)
    data = tf.cond(flip_lr > 0, lambda: data[:, :, ::-1, :], lambda: data)

    # input normalization
    scales = [10, 1000, 10000, 10000, 1000, 1]
    data = tf.stack([data[..., i] * scales[i] for i in range(filters)], axis=-1)

    # get logits
    if model == 'logistic':
        logits = tf.layers.conv2d(data[..., :-1], 1, 1, 1, padding='same')
    else:
        _, logits = tf_models.hourglass_cnn(data[..., :-1], res, min_res, res, num_filters)
    # segmentation logloss
    loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(labels=data[..., -1:],
                                                                  logits=logits))

    # actual predictions
    preds = tf.sigmoid(logits)
    preds = tf.cond(flip_lr > 0, lambda: preds[:, :, ::-1, :], lambda: preds)
    preds = preds[:, padding[1][0]:-padding[1][1]-1, padding[2][0]:-padding[2][0]-1, :]
    preds = tf.squeeze(tf.image.resize_images(preds, [height, width]))

    # optimization
    optimizer = tf.train.AdamOptimizer(learning_rate=1e-3)
    train_step = optimizer.minimize(loss)
    train_summary = tf.summary.scalar('train_loss', loss)

    saver = tf.train.Saver()
    model_path = os.getcwd() + '/model.ckpt'

    _, _, dset_all = get_mask_training_data()
    dset_train = dset_all[:80]
    dset_valid = dset_all[80:]

    def predict(dset, num_sample=16):
        with tf.Session() as sess:
            saver.restore(sess, model_path)
            for cur_data in tqdm.tqdm(dset):
                cur_data = np.concatenate([cur_data, np.zeros(cur_data.shape[:-1] + (1,))], axis=-1)
                pred = np.zeros((angles, height, width))
                for _ in range(num_sample):
                    pred += sess.run(preds, feed_dict={data_in: cur_data})
                yield pred / num_sample

    if os.path.exists('done'):
        return predict

    with read_log_dir():
        writer = tf.summary.FileWriter(os.getcwd())

    def batch_gen(x):
        batch_size = 1
        for i in tqdm.trange(0, len(x), batch_size):
            yield x[i:i+batch_size]

    def eval_model(sess):
        losses = []
        for cur_data in batch_gen(dset_valid):
            losses.append(sess.run(loss, feed_dict={data_in: cur_data}))
        return np.mean(losses)

    def train_model(sess):
        it = 0
        t0 = time.time()
        best_valid_loss = None
        while time.time() - t0 < duration * 3600:
            for cur_data in batch_gen(dset_train):
                _, cur_train_summary = sess.run([train_step, train_summary], feed_dict={
                    data_in: cur_data
                })
                writer.add_summary(cur_train_summary, it)
                it += 1

            valid_loss = eval_model(sess)
            cur_valid_summary = tf.Summary()
            cur_valid_summary.value.add(tag='valid_loss', simple_value=valid_loss)
            writer.add_summary(cur_valid_summary, it)

            if best_valid_loss is None or valid_loss < best_valid_loss:
                best_valid_loss = valid_loss
                saver.save(sess, model_path)

    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        train_model(sess)

    open('done', 'w').close()

    return predict


@cached(train_mask_segmentation_cnn, get_a3d_projection_data, cloud_cache=True,
        subdir='ssd', version=0)
def get_depth_maps(mode):
    if not os.path.exists('done'):
        names, labels, dset_in = get_a3d_projection_data(mode, 97)
        predict = train_mask_segmentation_cnn(0.1, model='hourglass', num_filters=64)

        f = h5py.File('data.hdf5', 'w')
        dset = f.create_dataset('dset', dset_in.shape[:-1])

        for i, (data, mask) in enumerate(zip(dset_in, predict(dset_in))):
            dset[i] = data[..., 0] * (mask > 0.5) * 2 + (mask <= 0.5)

        with open('pkl', 'wb') as f:
            pickle.dump((names, labels), f)
        open('done', 'w').close()
    else:
        with open('pkl', 'rb') as f:
            names, labels = pickle.load(f)
        f = h5py.File('data.hdf5', 'r')
        dset = f['dset']
    return names, labels, dset


@cached(synthetic_data.render_synthetic_zone_data, get_depth_maps, subdir='ssd', version=3)
def get_normalized_synthetic_zone_data(mode):
    if not os.path.exists('done'):
        _, _, dset_in = get_depth_maps(mode)
        dset = synthetic_data.render_synthetic_zone_data(mode)
        f = h5py.File('data.hdf5', 'w')
        dset_out = f.create_dataset('dset', dset.shape)

        def corners(image):
            coords = np.argwhere(image < 1)
            return np.min(coords, axis=0), np.max(coords, axis=0)

        def stats(image):
            (x0, y0), (x1, y1) = corners(image)
            valid = image[image < 1]
            mean, std = np.mean(valid), np.std(valid)
            return x1-x0, y1-y0, mean, std

        def distributions(gen):
            all_stats = [stats(data) for data in gen]
            h, w = [x[0] for x in all_stats], [x[1] for x in all_stats]
            mean_h, std_h = np.mean(h), np.std(h)
            mean_w, std_w = np.mean(w), np.std(w)
            mean_v = np.mean([x[2] for x in all_stats])
            std_v = np.sqrt(np.sum([x[3]**2 for x in all_stats]))
            return np.array([
                [mean_h, std_h],
                [mean_w, std_w],
                [mean_v, std_v]
            ])

        def dset_in_gen(angle):
            for data in dset_in:
                yield data[angle]

        def dset_gen(angle):
            for data in dset:
                yield data[angle, ..., 0]

        for angle in tqdm.trange(16):
            distr_in = distributions(dset_in_gen(angle))
            distr = distributions(dset_gen(angle))

            for i in tqdm.trange(len(dset)):
                (x0, y0), (x1, y1) = corners(dset[i, angle, ..., 0])
                crop = dset[i, angle, x0:x1, y0:y1]
                h, w = crop.shape[:2]

                hz, wz = (h-distr[0, 0])/distr[0, 1], (w-distr[1, 0])/distr[1, 1]
                hp, wp = hz*distr_in[0, 1]+distr_in[0, 0], wz*distr_in[1, 1]+distr_in[1, 0]
                resized = skimage.transform.resize(crop, (int(hp), int(wp)), preserve_range=True)
                resized = resized[1:-1, 1:-1]
                h_pad, w_pad = 330-resized.shape[0], (256-resized.shape[1])//2
                normal = np.stack([
                    np.pad(x, ((h_pad, 0), (w_pad, 256-resized.shape[1]-w_pad)), 'constant',
                           constant_values=y)
                    for x, y in [(resized[..., 0], 1), (resized[..., 1], 0)]
                ], axis=-1)

                depth = normal[..., 0]
                valid = depth < 1
                depth[valid] = (depth[valid]-distr[2, 0])/distr[2, 1]*distr_in[2, 1]+distr_in[2, 0]
                normal[..., 0] = depth

                dset_out[i, angle] = normal

        open('done', 'w').close()
    else:
        f = h5py.File('data.hdf5', 'r')
        dset_out = f['dset']
    return dset_out
                                   