'''
Tensorflow implementation of GraphFM described in:
GraphFM: Graph Factorization Machines for Feature Interaction Modeling
'''

import os
import numpy as np
import tensorflow as tf
from time import time
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.metrics import roc_auc_score, log_loss
# from tensorflow.contrib.layers.python.layers import batch_norm as batch_norm
# from tensorflow.contrib.rnn import GRUCell

'''
The following two functions are adapted from kyubyong park's implementation of transformer
We slightly modify the code to make it suitable for our work.(add relu, delete key masking and causality mask)
June 2017 by kyubyong park. 
kbpark.linguist@gmail.com.
https://www.github.com/kyubyong/transformer
'''


def normalize(inputs, epsilon=1e-8):
    '''
    Applies layer normalization
    Args:
        inputs: A tensor with 2 or more dimensions
        epsilon: A floating number to prevent Zero Division
    Returns:
        A tensor with the same shape and data dtype
    '''
    inputs_shape = inputs.get_shape()
    params_shape = inputs_shape[-1:]

    mean, variance = tf.nn.moments(inputs, [-1], keep_dims=True)
    beta = tf.Variable(tf.zeros(params_shape))
    gamma = tf.Variable(tf.ones(params_shape))
    normalized = (inputs - mean) / ((variance + epsilon) ** (.5))
    outputs = gamma * normalized + beta

    return outputs


def gat_attention(queries,
                  values,
                  num_units=None,
                  num_heads=1,
                  dropout_keep_prob=1,
                  k=1,
                  block=0,
                  field=0,
                  is_training=True,
                  has_residual=True):

    if num_units is None:
        num_units = queries.get_shape().as_list[-1]

    # Linear projections
    with tf.compat.v1.variable_scope("att_blocks%d" % block):
        if field > 0: tf.compat.v1.get_variable_scope().reuse_variables()
        A = tf.compat.v1.layers.dense(values, num_heads, activation=tf.nn.relu,
                            name='att_b%d_f%d' % (block, 0))
    with tf.compat.v1.variable_scope("w_blocks%d" % block):
        # if field > 0: tf.compat.v1.get_variable_scope().reuse_variables()
        H = tf.compat.v1.layers.dense(values, num_units, activation=None, use_bias=False, name='w_b%d_f%d' % (block, field))
        # A = tf.compat.v1.layers.dense(H, num_heads, activation=tf.nn.relu, name='att_b%d_f%d'%(block, field))
    if has_residual:
        with tf.compat.v1.variable_scope("res_blocks%d" % block):
            # if field > 0: tf.compat.v1.get_variable_scope().reuse_variables()
            Q_res = tf.compat.v1.layers.dense(queries, num_units, activation=tf.nn.relu,
                                    name='res_b%d_f%d' % (block, field))  # [batch_size, 1, num_units]

    # Split and concat
    A_ = tf.concat(tf.split(A, num_heads, axis=2), axis=0) # [num_heads*batch_size, field_size, 1]
    H_ = tf.concat(tf.split(H, num_heads, axis=2), axis=0)  # [num_heads*batch_size, field_size, num_units/num_heads]

    # keep the top k nodes
    with tf.compat.v1.variable_scope("gsl_blocks%d" % block):
        if field > 0: tf.compat.v1.get_variable_scope().reuse_variables()
        S = tf.compat.v1.layers.dense(values, 16, activation=tf.nn.relu, name='gsl_1_b%d' % (block))
        S = tf.compat.v1.layers.dense(S, 1, activation=tf.nn.sigmoid,
                            name='gsl_2_b%d' % (block))  # [batch_size, field_size, 1]
        S = tf.squeeze(S)  # [batch_size, field_size]
        values, indices = tf.nn.top_k(S, k=k)
        kth = tf.reduce_min(values, -1, keepdims=True)
        topk = tf.cast(tf.greater_equal(S, kth), tf.float32)
        S = S * topk
        S = tf.expand_dims(S, -1)  # [batch_size, field_size, 1]
        vis = tf.transpose(S, [0, 2, 1])  # [batch_size, 1, field_size]
        S = tf.tile(S, [num_heads, 1, 1])  # [num_heads*batch_size, field_size, 1]
        A_ = S * A_

    # Activation
    weights = tf.nn.softmax(A_, axis=1)

    # Dropouts
    weights = tf.keras.layers.dropout(weights, rate=1-dropout_keep_prob,
                                        training=tf.convert_to_tensor(is_training))

    # Weighted sum
    outputs = tf.multiply(weights, H_) # [num_heads*batch_size, field_size, num_units/num_heads]
    outputs = tf.reduce_sum(outputs, axis=1, keepdims=True)  # [num_heads*batch_size, 1, num_units/num_heads]

    # Restore shape
    outputs = tf.concat(tf.split(outputs, num_heads, axis=0), axis=2) # [batch_size, 1, num_units]

    # Residual connection
    if has_residual:
        outputs += Q_res

    outputs = tf.nn.relu(outputs)
    # Normalize
    outputs = normalize(outputs)

    return outputs, vis


class GraphFM():
    def __init__(self, args, feature_size, run_cnt):

        self.feature_size = feature_size  # denote as n, dimension of concatenated features
        self.field_size = args.field_size  # denote as M, number of total feature fields
        self.embedding_size = args.embedding_size  # denote as d, size of the feature embedding
        self.blocks = args.blocks  # number of the blocks
        self.heads = args.heads  # number of the heads
        self.block_shape = args.block_shape
        self.output_size = args.block_shape[-1]
        self.has_residual = args.has_residual
        self.deep_layers = args.deep_layers  # whether to joint train with deep networks as described in paper

        self.k = args.k
        self.ks = args.ks

        self.batch_norm = args.batch_norm
        self.batch_norm_decay = args.batch_norm_decay
        self.drop_keep_prob = args.dropout_keep_prob
        self.l2_reg = args.l2_reg
        self.epoch = args.epoch
        self.batch_size = args.batch_size
        self.learning_rate = args.learning_rate
        self.optimizer_type = args.optimizer_type

        self.data_path = args.data_path

        self.save_path = args.save_path + str(run_cnt) + '/'
        self.is_save = args.is_save
        if (args.is_save == True and os.path.exists(self.save_path) == False):
            os.makedirs(self.save_path)

        self.verbose = args.verbose
        self.random_seed = args.random_seed
        self.loss_type = args.loss_type
        self.eval_metric = roc_auc_score
        self.best_loss = 1.0
        self.greater_is_better = args.greater_is_better
        self.train_result, self.valid_result = [], []
        self.train_loss, self.valid_loss = [], []

        self._init_graph()

    def _init_graph(self):
        self.graph = tf.Graph()
        with self.graph.as_default():

            tf.random.set_seed(self.random_seed)

            self.feat_index = tf.compat.v1.placeholder(tf.int32, shape=[None, None],
                                             name="feat_index")  # None * M
            self.feat_value = tf.compat.v1.placeholder(tf.float32, shape=[None, None],
                                             name="feat_value")  # None * M
            self.label = tf.compat.v1.placeholder(tf.float32, shape=[None, 1], name="label")  # None * 1

            # In our implementation, the shape of dropout_keep_prob is [3], used in 3 different places.
            self.dropout_keep_prob = tf.compat.v1.placeholder(tf.float32, shape=[None], name="dropout_keep_prob")
            self.train_phase = tf.compat.v1.placeholder(tf.bool, name="train_phase")

            self.weights = self._initialize_weights()

            # model
            self.embeddings = tf.nn.embedding_lookup(self.weights["feature_embeddings"],
                                                     self.feat_index)  # None * M * d
            feat_value = tf.reshape(self.feat_value, shape=[-1, self.field_size, 1])
            self.embeddings = tf.multiply(self.embeddings, feat_value)  # None * M * d
            self.embeddings = tf.nn.dropout(self.embeddings, self.dropout_keep_prob[1])  # None * M * d

            # joint training with feedforward nn
            if self.deep_layers != None:
                self.y_dense = tf.reshape(self.embeddings, shape=[-1, self.field_size * self.embedding_size])
                for i in range(0, len(self.deep_layers)):
                    self.y_dense = tf.add(tf.matmul(self.y_dense, self.weights["layer_%d" % i]),
                                          self.weights["bias_%d" % i])  # None * layer[i]
                    if self.batch_norm:
                        self.y_dense = self.batch_norm_layer(self.y_dense, train_phase=self.train_phase,
                                                             scope_bn="bn_%d" % i)
                    self.y_dense = tf.nn.relu(self.y_dense)
                    self.y_dense = tf.nn.dropout(self.y_dense, self.dropout_keep_prob[2])
                self.y_dense = tf.add(tf.matmul(self.y_dense, self.weights["prediction_dense"]),
                                      self.weights["prediction_bias_dense"], name='logits_dense')  # None * 1

            # ---------- main part of GAIN_gsl -------------------
            self.y_deep = self.embeddings  # None * M * d
            h = self.y_deep
            h_list = []
            v_list = []
            for i in range(self.blocks):
                state_list = []
                vis_list = []
                for j in range(self.field_size):
                    state = tf.expand_dims(h[:, j, :], 1)  # [batch_size, 1, hidden_size]
                    state_all = state * h  # [batch_size, field_size, hidden_size]
                    state, vis = gat_attention(queries=state,
                                          values=state_all,
                                          num_units=self.block_shape[i],
                                          num_heads=self.heads,
                                          k=self.ks[i],
                                          block=i, field=j,
                                          dropout_keep_prob=self.dropout_keep_prob[0],
                                          is_training=self.train_phase,
                                          has_residual=self.has_residual)
                    state_list.append(state)
                    vis_list.append(vis)
                h = tf.concat(state_list, axis=1)  # [batch_size, field_size, block_shape[i]]
                v = tf.concat(vis_list, axis=1)  # [batch_size, field_size, field_size]
                h_list.append(h)
                v_list.append(v)

            self.v_list = v_list = tf.transpose(tf.stack(v_list, axis=0),
                                                [1, 0, 2, 3])  # [batch_size, blocks, field_size, field_size]

            # only use the last layer
            # self.y_deep = h

            # mean_pooling of all the layer
            # ngcf form
            self.y_deep = tf.concat(h_list, axis=-1)
            self.flat = tf.reduce_mean(self.y_deep, axis=1, keep_dims=False)

            self.out = tf.add(tf.matmul(self.flat, self.weights["prediction"]),
                              self.weights["prediction_bias"], name='logits')  # None * 1
            if self.deep_layers != None:
                self.out += self.y_dense

            # ---------- Compute the loss ----------
            # loss
            if self.loss_type == "logloss":
                self.out = tf.nn.sigmoid(self.out, name='pred')
                self.loss_fn = tf.keras.losses.LogCosh()
                self.loss = self.loss_fn(self.label, self.out)
                # self.loss = tf.losses.log_loss(self.label, self.out)
            elif self.loss_type == "mse":
                self.loss = tf.nn.l2_loss(tf.subtract(self.label, self.out))

            # l2 regularization on weights
            if self.l2_reg > 0:
                if self.deep_layers != None:
                    for i in range(len(self.deep_layers)):
                        self.loss += tf.contrib.layers.l2_regularizer(
                            self.l2_reg)(self.weights["layer_%d" % i])

            self.global_step = tf.Variable(0, name="global_step", trainable=False)
            self.var1 = [v for v in tf.trainable_variables() if v.name != 'feature_bias:0']
            self.var2 = [tf.trainable_variables()[1]]  # self.var2 = [feature_bias]

            if self.optimizer_type == "adam":
                self.optimizer = tf.compat.v1.train.AdamOptimizer(learning_rate=self.learning_rate,
                                                        beta1=0.9, beta2=0.999, epsilon=1e-8). \
                    minimize(self.loss, global_step=self.global_step)
            elif self.optimizer_type == "adagrad":
                self.optimizer = tf.compat.v1.train.AdagradOptimizer(learning_rate=self.learning_rate,
                                                           initial_accumulator_value=1e-8). \
                    minimize(self.loss, global_step=self.global_step)
            elif self.optimizer_type == "gd":
                self.optimizer = tf.compat.v1.train.GradientDescentOptimizer(learning_rate=self.learning_rate). \
                    minimize(self.loss, global_step=self.global_step)
            elif self.optimizer_type == "momentum":
                self.optimizer = tf.compat.v1.train.MomentumOptimizer(learning_rate=self.learning_rate, momentum=0.95). \
                    minimize(self.loss, global_step=self.global_step)

            # init
            self.saver = tf.compat.v1.train.Saver(max_to_keep=5)
            init = tf.compat.v1.global_variables_initializer()
            self.sess = self._init_session()
            self.sess.run(init)
            self.count_param()

    def count_param(self):
        k = (np.sum([np.prod(v.get_shape().as_list())
                     for v in tf.trainable_variables()]))

        print("total parameters :%d" % k)
        print("extra parameters : %d" % (k - self.feature_size * self.embedding_size))

    def _init_session(self):
        config = tf.compat.v1.ConfigProto(allow_soft_placement=True)
        config.gpu_options.allow_growth = True
        return tf.compat.v1.Session(config=config)

    def _initialize_weights(self):
        weights = dict()

        # embeddings
        weights["feature_embeddings"] = tf.Variable(
            tf.random.normal([self.feature_size, self.embedding_size], 0.0, 0.01),
            name="feature_embeddings")  # feature_size(n) * d

        input_size = sum(self.block_shape)

        # dense layers
        if self.deep_layers != None:
            num_layer = len(self.deep_layers)
            layer0_size = self.field_size * self.embedding_size
            glorot = np.sqrt(2.0 / (layer0_size + self.deep_layers[0]))
            weights["layer_0"] = tf.Variable(
                np.random.normal(loc=0, scale=glorot, size=(layer0_size, self.deep_layers[0])), dtype=np.float32)
            weights["bias_0"] = tf.Variable(np.random.normal(loc=0, scale=glorot, size=(1, self.deep_layers[0])),
                                            dtype=np.float32)  # 1 * layers[0]
            for i in range(1, num_layer):
                glorot = np.sqrt(2.0 / (self.deep_layers[i - 1] + self.deep_layers[i]))
                weights["layer_%d" % i] = tf.Variable(
                    np.random.normal(loc=0, scale=glorot, size=(self.deep_layers[i - 1], self.deep_layers[i])),
                    dtype=np.float32)  # layers[i-1] * layers[i]
                weights["bias_%d" % i] = tf.Variable(
                    np.random.normal(loc=0, scale=glorot, size=(1, self.deep_layers[i])),
                    dtype=np.float32)  # 1 * layer[i]
            glorot = np.sqrt(2.0 / (self.deep_layers[-1] + 1))
            weights["prediction_dense"] = tf.Variable(
                np.random.normal(loc=0, scale=glorot, size=(self.deep_layers[-1], 1)),
                dtype=np.float32, name="prediction_dense")
            weights["prediction_bias_dense"] = tf.Variable(
                np.random.normal(), dtype=np.float32, name="prediction_bias_dense")

        # ---------- prediciton weight ------------------#
        glorot = np.sqrt(2.0 / (input_size + 1))
        weights["prediction"] = tf.Variable(
            np.random.normal(loc=0, scale=glorot, size=(input_size, 1)),
            dtype=np.float32, name="prediction")
        weights["prediction_bias"] = tf.Variable(
            np.random.normal(), dtype=np.float32, name="prediction_bias")

        return weights

    def batch_norm_layer(self, x, train_phase, scope_bn):
        bn_train = tf.keras.layers.BatchNormalization(x, decay=self.batch_norm_decay, center=True, scale=True, updates_collections=None,
                              is_training=True, reuse=None, trainable=True, scope=scope_bn)
        bn_inference = tf.keras.layers.BatchNormalization(x, decay=self.batch_norm_decay, center=True, scale=True, updates_collections=None,
                                  is_training=False, reuse=True, trainable=True, scope=scope_bn)
        z = tf.cond(train_phase, lambda: bn_train, lambda: bn_inference)
        return z

    def get_batch(self, Xi, Xv, y, batch_size, index):
        start = index * batch_size
        end = (index + 1) * batch_size
        end = end if end < len(y) else len(y)
        return Xi[start:end], Xv[start:end], [[y_] for y_ in y[start:end]]

    # shuffle three lists simutaneously
    def shuffle_in_unison_scary(self, a, b, c):
        rng_state = np.random.get_state()
        np.random.shuffle(a)
        np.random.set_state(rng_state)
        np.random.shuffle(b)
        np.random.set_state(rng_state)
        np.random.shuffle(c)

    def fit_on_batch(self, Xi, Xv, y):
        feed_dict = {self.feat_index: Xi,
                     self.feat_value: Xv,
                     self.label: y,
                     self.dropout_keep_prob: self.drop_keep_prob,
                     self.train_phase: True}
        step, loss, opt = self.sess.run((self.global_step, self.loss, self.optimizer), feed_dict=feed_dict)
        return step, loss

    # Since the train data is very large, they can not be fit into the memory at the same time.
    # We separate the whole train data into several files and call "fit_once" for each file.
    def fit_once(self, Xi_train, Xv_train, y_train,
                 epoch, file_count, Xi_valid=None,
                 Xv_valid=None, y_valid=None,
                 early_stopping=False):

        has_valid = Xv_valid is not None
        last_step = 0
        t1 = time()
        self.shuffle_in_unison_scary(Xi_train, Xv_train, y_train)
        total_batch = int(len(y_train) / self.batch_size)
        for i in range(total_batch):
            Xi_batch, Xv_batch, y_batch = self.get_batch(Xi_train, Xv_train, y_train, self.batch_size, i)
            step, loss = self.fit_on_batch(Xi_batch, Xv_batch, y_batch)
            last_step = step

        # evaluate training and validation datasets
        train_result, train_loss = self.evaluate(Xi_train, Xv_train, y_train)
        self.train_result.append(train_result)
        self.train_loss.append(train_loss)
        if has_valid:
            valid_result, valid_loss = self.evaluate(Xi_valid, Xv_valid, y_valid)
            self.valid_result.append(valid_result)
            self.valid_loss.append(valid_loss)
            if valid_loss < self.best_loss and self.is_save == True:
                old_loss = self.best_loss
                self.best_loss = valid_loss
                self.saver.save(self.sess, self.save_path + 'model.ckpt', global_step=last_step)
                print("[%d-%d] model saved!. Valid loss is improved from %.4f to %.4f"
                      % (epoch, file_count, old_loss, self.best_loss))

        if self.verbose > 0 and ((epoch - 1) * 9 + file_count) % self.verbose == 0:
            if has_valid:
                print(
                    "[%d-%d] train-result=%.4f, train-logloss=%.4f, valid-result=%.4f, valid-logloss=%.4f [%.1f s]" % (
                    epoch, file_count, train_result, train_loss, valid_result, valid_loss, time() - t1))
            else:
                print("[%d-%d] train-result=%.4f [%.1f s]" \
                      % (epoch, file_count, train_result, time() - t1))
        if has_valid and early_stopping and self.training_termination(self.valid_loss):
            return False
        else:
            return True

    def training_termination(self, valid_result):
        if len(valid_result) > 5:
            if self.greater_is_better:
                if valid_result[-1] < valid_result[-2] and \
                        valid_result[-2] < valid_result[-3] and \
                        valid_result[-3] < valid_result[-4] and \
                        valid_result[-4] < valid_result[-5]:
                    return True
            else:
                if valid_result[-1] > valid_result[-2] and \
                        valid_result[-2] > valid_result[-3] and \
                        valid_result[-3] > valid_result[-4] and \
                        valid_result[-4] > valid_result[-5]:
                    return True
        return False

    def predict(self, Xi, Xv):
        """
        :param Xi: list of list of feature indices of each sample in the dataset
        :param Xv: list of list of feature values of each sample in the dataset
        :return: predicted probability of each sample
        """
        # dummy y
        dummy_y = [1] * len(Xi)
        batch_index = 0
        Xi_batch, Xv_batch, y_batch = self.get_batch(Xi, Xv, dummy_y, self.batch_size, batch_index)
        y_pred = None
        while len(Xi_batch) > 0:
            num_batch = len(y_batch)
            feed_dict = {self.feat_index: Xi_batch,
                         self.feat_value: Xv_batch,
                         self.label: y_batch,
                         self.dropout_keep_prob: [1.0] * len(self.drop_keep_prob),
                         self.train_phase: False}
            batch_out = self.sess.run(self.out, feed_dict=feed_dict)

            if batch_index == 0:
                y_pred = np.reshape(batch_out, (num_batch,))
            else:
                y_pred = np.concatenate((y_pred, np.reshape(batch_out, (num_batch,))))

            batch_index += 1
            Xi_batch, Xv_batch, y_batch = self.get_batch(Xi, Xv, dummy_y, self.batch_size, batch_index)

        return y_pred

    def visualize(self, Xi, Xv, y):
        """
        :param Xi: list of list of feature indices of each sample in the dataset
        :param Xv: list of list of feature values of each sample in the dataset
        :return: predicted probability of each sample
        """

        # for visualization
        visualization = []

        # dummy y
        dummy_y = [1] * len(Xi)
        batch_index = 0
        Xi_batch, Xv_batch, y_batch = self.get_batch(Xi, Xv, dummy_y, self.batch_size, batch_index)
        y_pred = None
        while len(Xi_batch) > 0:
            num_batch = len(y_batch)
            feed_dict = {self.feat_index: Xi_batch,
                         self.feat_value: Xv_batch,
                         self.label: y_batch,
                         self.dropout_keep_prob: [1.0] * len(self.drop_keep_prob),
                         self.train_phase: False}
            batch_out, batch_vis = self.sess.run([self.out, self.v_list], feed_dict=feed_dict)


            if batch_index == 0:
                y_pred = np.reshape(batch_out, (num_batch,))
                vis = np.reshape(batch_vis, (num_batch, self.blocks, self.field_size, self.field_size))
            else:
                y_pred = np.concatenate((y_pred, np.reshape(batch_out, (num_batch,))))
                vis = np.concatenate(
                    (vis, np.reshape(batch_vis, (num_batch, self.blocks, self.field_size, self.field_size))))

            # record the sample along with the visualization
            for i in range(num_batch):
                l = []
                l.append(Xi_batch[i])
                l.append(Xv_batch[i])
                l.append(y_batch[i])
                l.append(batch_out[i])
                l.append(batch_vis[i])
                visualization.append(l)

            batch_index += 1
            Xi_batch, Xv_batch, y_batch = self.get_batch(Xi, Xv, dummy_y, self.batch_size, batch_index)

        np.save(self.data_path+"/visualization-" + str(self.ks) + ".npy", visualization)

        return visualization

    def evaluate(self, Xi, Xv, y):
        """
        :param Xi: list of list of feature indices of each sample in the dataset
        :param Xv: list of list of feature values of each sample in the dataset
        :param y: label of each sample in the dataset
        :return: metric of the evaluation
        """
        y_pred = self.predict(Xi, Xv)
        y_pred = np.clip(y_pred, 1e-6, 1 - 1e-6)
        return self.eval_metric(y, y_pred), log_loss(y, y_pred)

    def restore(self, save_path=None):
        if (save_path == None):
            save_path = self.save_path
        ckpt = tf.train.get_checkpoint_state(save_path)
        if ckpt and ckpt.model_checkpoint_path:
            self.saver.restore(self.sess, ckpt.model_checkpoint_path)
            if self.verbose > 0:
                print("restored from %s" % (save_path))