# Copyright 2019 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""SketchRNN training."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from cStringIO import StringIO
import json
import os
import time
import urllib
import zipfile

from IPython.core.debugger import set_trace
import numpy as np
import requests
import tensorflow as tf
import matplotlib.pyplot as plt

import model as sketch_rnn_model
import utils_class as utils
tf.logging.set_verbosity(tf.logging.INFO)

FLAGS = tf.app.flags.FLAGS

tf.app.flags.DEFINE_string(
    'data_dir',
    '.',
    # 'https://github.com/hardmaru/sketch-rnn-datasets/raw/master/',
    'The directory in which to find the dataset specified in model hparams. '
    'If data_dir starts with "http://" or "https://", the file will be fetched '
    'remotely.')
tf.app.flags.DEFINE_string(
    'log_root', '/tmp/sketch_rnn/models/default',
    'Directory to store model checkpoints, tensorboard.')
tf.app.flags.DEFINE_boolean(
    'resume_training', False,
    'Set to true to load previous checkpoint')
tf.app.flags.DEFINE_string(
    'hparams', '',
    'Pass in comma-separated key=value pairs such as '
    '\'save_every=40,decay_rate=0.99\' '
    '(no whitespace) to be read into the HParams object defined in model.py')

PRETRAINED_MODELS_URL = ('http://download.magenta.tensorflow.org/models/'
                         'sketch_rnn.zip')

# Store the cost values and time_taken once every 20 epochs
cost_list = []
time_taken_list = []
# Store the current accuracy values once every 50 epochs
accuracy_list = []
  
def reset_graph():
  """Closes the current default session and resets the graph."""
  sess = tf.get_default_session()
  if sess:
    sess.close()
  tf.reset_default_graph()

def load_dataset(data_dir, model_params, inference_mode=False):
  """Loads the .npz file, and splits the set into train/valid/test."""

  # normalizes the x and y columns usint the training set.
  # applies same scaling factor to valid and test set.

  datasets = []
  if isinstance(model_params.data_set, list):
    datasets = model_params.data_set
  else:
    datasets = [model_params.data_set]

  train_strokes = None
  valid_strokes = None
  test_strokes = None
  train_y = None
  valid_y = None
  test_y = None
  for idx, dataset in enumerate(datasets):
    data_filepath = os.path.join(data_dir, dataset)
    if data_dir.startswith('http://') or data_dir.startswith('https://'):
      tf.logging.info('Downloading %s', data_filepath)
      response = requests.get(data_filepath)
      data = np.load(StringIO(response.content))
    else:
      data = np.load(data_filepath)  # load this into dictionary
    tf.logging.info('Loaded {}/{}/{} from {}'.format(
        len(data['train']), len(data['valid']), len(data['test']),
        dataset))
    if train_strokes is None:
      train_strokes = data['train']
      valid_strokes = data['valid']
      test_strokes = data['test']
      train_y = [idx]*len(train_strokes)
      valid_y = [idx]*len(valid_strokes)
      test_y = [idx]*len(test_strokes)
    else:
      train_strokes = np.concatenate((train_strokes, data['train']))
      valid_strokes = np.concatenate((valid_strokes, data['valid']))
      test_strokes = np.concatenate((test_strokes, data['test']))
      train_y = np.concatenate((train_y, [idx]*len(data['train'])))
      valid_y = np.concatenate((valid_y, [idx]*len(data['valid'])))
      test_y = np.concatenate((test_y, [idx]*len(data['test'])))
  all_strokes = np.concatenate((train_strokes, valid_strokes, test_strokes))
  num_points = 0
  for stroke in all_strokes:
    num_points += len(stroke)
  avg_len = num_points / len(all_strokes)
  tf.logging.info('Dataset combined: {} ({}/{}/{}), avg len {}'.format(
      len(all_strokes), len(train_strokes), len(valid_strokes),
      len(test_strokes), int(avg_len)))

  # calculate the max strokes we need.
  max_seq_len = utils.get_max_len(all_strokes)
  # overwrite the hps with this calculation.
  model_params.max_seq_len = max_seq_len

  tf.logging.info('model_params.max_seq_len %i.', model_params.max_seq_len)

  eval_model_params = sketch_rnn_model.copy_hparams(model_params)

  eval_model_params.use_input_dropout = 0
  eval_model_params.use_recurrent_dropout = 0
  eval_model_params.use_output_dropout = 0
  eval_model_params.is_training = 1

  if inference_mode:
    eval_model_params.batch_size = 1
    eval_model_params.is_training = 0

  sample_model_params = sketch_rnn_model.copy_hparams(eval_model_params)
  sample_model_params.batch_size = 1  # only sample one at a time
  sample_model_params.max_seq_len = 1  # sample one point at a time
  
  train_set = utils.DataLoader(
      strokes=train_strokes, labels=train_y,
      batch_size=model_params.batch_size,
      max_seq_length=model_params.max_seq_len,
      random_scale_factor=model_params.random_scale_factor,
      augment_stroke_prob=model_params.augment_stroke_prob)

  normalizing_scale_factor = train_set.calculate_normalizing_scale_factor()
  train_set.normalize(normalizing_scale_factor)

  valid_set = utils.DataLoader(
      strokes=valid_strokes,
      labels=valid_y,
      batch_size=eval_model_params.batch_size,
      max_seq_length=eval_model_params.max_seq_len,
      random_scale_factor=0.0,
      augment_stroke_prob=0.0)
  valid_set.normalize(normalizing_scale_factor)

  test_set = utils.DataLoader(
      strokes=test_strokes,
      labels=test_y,
      batch_size=eval_model_params.batch_size,
      max_seq_length=eval_model_params.max_seq_len,
      random_scale_factor=0.0,
      augment_stroke_prob=0.0)
  test_set.normalize(normalizing_scale_factor)

  tf.logging.info('normalizing_scale_factor %4.4f.', normalizing_scale_factor)

  result = [
      train_set, valid_set, test_set, model_params, eval_model_params,
      sample_model_params
  ]
  return result

def evaluate_model(sess, model, data_set):
  """Returns the average weighted cost, reconstruction cost and KL cost."""
  total_cost = 0.0
  pred_arr = None
  for batch in range(data_set.num_batches):
    unused_orig_x, lab_v, x, s = data_set.get_batch(batch)
    # print (x)
    feed = {model.input_data: x, model.y_labels: lab_v, model.sequence_lengths: s}
    (cost,pred) = sess.run([model.cost, model.output], feed)
    if pred_arr is None:
      pred_arr = np.array(pred)
    else:
      pred_arr = np.concatenate((pred_arr, pred))
    total_cost += cost

  total_cost /= (data_set.num_batches)
  return total_cost, pred_arr


def load_checkpoint(sess, checkpoint_path):
  saver = tf.train.Saver(tf.global_variables())
  #saver = tf.train.import_meta_graph('./checkpoints/vector-20000.meta', clear_devices=True)
  ckpt = tf.train.get_checkpoint_state(checkpoint_path)
  tf.logging.info('Loading model %s.', ckpt.model_checkpoint_path)
  saver.restore(sess, ckpt.model_checkpoint_path)


def save_model(sess, model_save_path, global_step):
  saver = tf.train.Saver(tf.global_variables())
  checkpoint_path = os.path.join(model_save_path, 'vector')
  tf.logging.info('saving model %s.', checkpoint_path)
  tf.logging.info('global_step %i.', global_step)
  saver.save(sess, checkpoint_path, global_step=global_step)


def train(sess, model, eval_model, train_set, valid_set, test_set):
  """Train a sketch-rnn model."""
  # Setup summary writer.
  summary_writer = tf.summary.FileWriter(FLAGS.log_root)

  # Calculate trainable params.
  t_vars = tf.trainable_variables()
  count_t_vars = 0
  for var in t_vars:
    num_param = np.prod(var.get_shape().as_list())
    count_t_vars += num_param
    tf.logging.info('%s %s %i', var.name, str(var.get_shape()), num_param)
  tf.logging.info('Total trainable variables %i.', count_t_vars)
  model_summ = tf.summary.Summary()
  model_summ.value.add(
      tag='Num_Trainable_Params', simple_value=float(count_t_vars))
  summary_writer.add_summary(model_summ, 0)
  summary_writer.flush()

  # setup eval stats
  best_valid_cost = 100000000.0  # set a large init value
  valid_cost = 0.0

  # main train loop

  hps = model.hps
  start = time.time()

  for _ in range(hps.num_steps):

    step = sess.run(model.global_step)

    curr_learning_rate = ((hps.learning_rate - hps.min_learning_rate) *
                          (hps.decay_rate)**step + hps.min_learning_rate)
    
    _, lab, x, s = train_set.random_batch()
    feed = {
        model.input_data: x,
        model.y_labels: lab,
        model.sequence_lengths: s,
        model.lr: curr_learning_rate,
    }

    (train_cost, _, train_step, _) = sess.run([
        model.cost, model.output,
        model.global_step, model.train_op
    ], feed)

    if step % 20 == 0 and step > 0:
      # Logging stuff here
      end = time.time()
      time_taken = end - start
      time_taken_list.append(time_taken)

      cost_summ = tf.summary.Summary()
      cost_summ.value.add(tag='Train_Cost', simple_value=float(train_cost))
      cost_list.append(float(train_cost))
      lr_summ = tf.summary.Summary()
      lr_summ.value.add(
          tag='Learning_Rate', simple_value=float(curr_learning_rate))
      
      time_summ = tf.summary.Summary()
      time_summ.value.add(
          tag='Time_Taken_Train', simple_value=float(time_taken))

      output_format = ('step: %d, lr: %.6f, cost: %f, train_time_taken: %.4f')
      output_values = (step, curr_learning_rate, train_cost, time_taken)
      output_log = output_format % output_values

      tf.logging.info(output_log)

      summary_writer.add_summary(cost_summ, train_step)
      summary_writer.add_summary(lr_summ, train_step)
      summary_writer.add_summary(time_summ, train_step)
      summary_writer.flush()
      start = time.time()

    if step % hps.save_every == 0 and step > 0:

      valid_cost, pred_v = evaluate_model(sess, eval_model, valid_set)
      pred_v = np.array(pred_v)
      pred_class = np.argmax(pred_v, axis=1)
      print (np.sum(pred_class==valid_set.labels))
      
      end = time.time()
      time_taken_valid = end - start
      start = time.time()

      valid_cost_summ = tf.summary.Summary()
      valid_cost_summ.value.add(
          tag='Valid_Cost', simple_value=float(valid_cost))
      
      valid_time_summ = tf.summary.Summary()
      valid_time_summ.value.add(
          tag='Time_Taken_Valid', simple_value=float(time_taken_valid))

      output_format = ('best_valid_cost: %0.4f, valid_cost: %.4f, valid_time_taken: %.4f')
      output_values = (min(best_valid_cost, valid_cost), valid_cost, time_taken_valid)
      output_log = output_format % output_values

      tf.logging.info(output_log)

      summary_writer.add_summary(valid_cost_summ, train_step)
      summary_writer.add_summary(valid_time_summ, train_step)
      summary_writer.flush()

      if valid_cost < best_valid_cost:
        best_valid_cost = valid_cost

        save_model(sess, FLAGS.log_root, step)

        end = time.time()
        time_taken_save = end - start
        start = time.time()

        tf.logging.info('time_taken_save %4.4f.', time_taken_save)

        best_valid_cost_summ = tf.summary.Summary()
        best_valid_cost_summ.value.add(
            tag='Best_Valid_Cost', simple_value=float(best_valid_cost))

        summary_writer.add_summary(best_valid_cost_summ, train_step)
        summary_writer.flush()

        eval_cost, pred_v = evaluate_model(sess, eval_model, test_set)
        pred_v = np.array(pred_v)
        pred_class = np.argmax(pred_v, axis=1)
        accuracy_val = np.sum(pred_class==test_set.labels)*100/len(test_set.labels)
        print ("==================================================")
        print ("=          Accuracy = ", accuracy_val, "         =")
        print ("==================================================")
        accuracy_list.append(accuracy_val)
        end = time.time()
        time_taken_eval = end - start
        start = time.time()

        eval_cost_summ = tf.summary.Summary()
        eval_cost_summ.value.add(tag='Eval_Cost', simple_value=float(eval_cost))
        eval_time_summ = tf.summary.Summary()
        eval_time_summ.value.add(
            tag='Time_Taken_Eval', simple_value=float(time_taken_eval))

        output_format = ('eval_cost: %.4f, eval_time_taken: %.4f')
        output_values = (eval_cost, time_taken_eval)
        output_log = output_format % output_values

        tf.logging.info(output_log)

        summary_writer.add_summary(eval_cost_summ, train_step)
        summary_writer.add_summary(eval_time_summ, train_step)
        summary_writer.flush()

def trainer(model_params):
  """Train a sketch-rnn model."""
  np.set_printoptions(precision=8, edgeitems=6, linewidth=200, suppress=True)

  tf.logging.info('sketch-rnn')
  tf.logging.info('Hyperparams:')
  for key, val in model_params.values().iteritems():
    tf.logging.info('%s = %s', key, str(val))
  tf.logging.info('Loading data files.')
  datasets = load_dataset(FLAGS.data_dir, model_params)

  train_set = datasets[0]
  valid_set = datasets[1]
  test_set = datasets[2]
  model_params = datasets[3]
  eval_model_params = datasets[4]

  reset_graph()
  model = sketch_rnn_model.Model(model_params)
  eval_model = sketch_rnn_model.Model(eval_model_params, reuse=True)

  sess = tf.InteractiveSession()
  sess.run(tf.global_variables_initializer())

  if FLAGS.resume_training:
    load_checkpoint(sess, FLAGS.log_root)

  # Write config file to json file.
  tf.gfile.MakeDirs(FLAGS.log_root)
  with tf.gfile.Open(
      os.path.join(FLAGS.log_root, 'model_config.json'), 'w') as f:
    json.dump(model_params.values(), f, indent=True)

  train(sess, model, eval_model, train_set, valid_set, test_set)


def main(unused_argv):
  """Load model params, save config file and start trainer."""
  model_params = sketch_rnn_model.get_default_hparams()
  if FLAGS.hparams:
    model_params.parse(FLAGS.hparams)
  trainer(model_params)
  # Plot graphs
  print ("Plotting the graphs\n")
  t = np.arange(0,20*len(cost_list),20)
  plt.subplot(211)
  plt.plot(t, cost_list , 'bo')
  plt.subplot(212)
  plt.plot(t, time_taken_list , 'bo')
  plt.savefig("cost_time.png")


def console_entry_point():
  tf.app.run(main)


if __name__ == '__main__':
  console_entry_point()