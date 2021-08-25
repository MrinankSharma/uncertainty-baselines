import os
import pdb
import time
from typing import Dict, List, Tuple, NamedTuple, Callable, Union

import numpy as np
import pandas as pd
import robustness_metrics as rm
import tensorflow as tf
import torch
from absl import flags
from absl import logging
from sklearn.metrics import (log_loss, roc_auc_score, accuracy_score,
                             roc_curve, precision_recall_curve, auc)
import datetime
import utils  # local file import


import matplotlib.pyplot as plt
import seaborn as sns
import os.path as osp


N_SAMPLES_PER_CLASS = {
  "aptos": [1805, 370, 999, 193, 295],
  "eyepacs": {
    "train": [25810, 2443, 5292, 873, 708],
    "validation": [8130, 720, 1579, 237, 240],
    "test": [31403, 3042, 6282, 977, 966],
  }
}


def plot_predictive_entropy_histogram(
    y_uncertainty: np.ndarray, is_ood: np.ndarray,
    uncertainty_type: str, in_dist_str='In-Domain', ood_str='OOD',
    title='', plt_path='.'):
  """
  Should pass all uncertainty scores for in-domain and OOD points.

  :param y_uncertainty:
  :param is_ood:
  :param in_dist_str:
  :param ood_str:
  :param title:
  :param plt_path:
  :return:
  """
  assert uncertainty_type in {'entropy', 'variance'}
  short_to_full_uncertainty_type = {
    'entropy': 'Predictive Entropy',
    'variance': 'Predictive Variance'
  }
  full_uncertainty_type = short_to_full_uncertainty_type[uncertainty_type]
  sns.displot({
      full_uncertainty_type: y_uncertainty,
      'Dataset': [ood_str if ood_label else in_dist_str for ood_label in is_ood]
    }, x=full_uncertainty_type, hue="Dataset")
  plt.savefig(osp.join(plt_path, f'predictive_{uncertainty_type}.svg'))


def plot_roc_curve(fpr, tpr, roc_auc, title='', plt_path='.'):
  """
  Based on scikit-learn examples.
  https://scikit-learn.org/stable/auto_examples/model_selection/
  plot_roc.html#sphx-glr-auto-examples-model-selection-plot-roc-py
  """
  plt.figure()
  lw = 2
  plt.plot(fpr, tpr, color='darkorange',
           lw=lw, label='ROC curve (area = %0.2f)' % roc_auc)
  plt.plot([0, 1], [0, 1], color='navy', lw=lw, linestyle='--')
  plt.xlim([0.0, 1.0])
  plt.ylim([0.0, 1.05])
  plt.xlabel('False Positive Rate')
  plt.ylabel('True Positive Rate')
  plt.title(f'{title} ROC Curve')
  plt.legend(loc="lower right")
  plt.savefig(osp.join(plt_path, 'roc_curve.svg'))


def plot_ece(y_true, y_pred, name="", n_windows=100, ax=None, color='blue'):
  """
  All inputs are 1D array
  """
  probs = binary_converter(y_pred).flatten()
  labels = one_hot_encode(y_true, 2).flatten()

  probs_labels = np.stack([probs, labels]).T
  probs_labels = probs_labels[np.argsort(probs)]

  window_len = int(len(probs_labels) / n_windows)
  confidences = []
  accuracies = []
  # distances = []
  for i in range(len(probs_labels) - window_len):
    # distances.append((probs_labels[i + window_len, 0] - probs_labels[i, 0]) / float(window_len))
    mean_confidences = mean(probs_labels[i:i + window_len, 0])
    confidences.append(mean_confidences)
    class_accuracies = mean(probs_labels[i:i + window_len, 1])
    accuracies.append(class_accuracies)

  if not ax:
    plt.figure()
    ax = plt.gca()

  # ax.figure.set_size_inches(6, 4)
  ax.plot(confidences, accuracies, color=color, label=name)

  xbins = [i / 10. for i in range(11)]
  ax.plot(xbins, xbins, linestyle=':', color='black')
  ax.set_xlabel('Model Confidence')
  ax.set_ylabel('Model Accuracy')
  # ax.set_title(f"Reliability Diagram Trained on {train_name}, Evaluated on {ood_name}")
  # ax.set_title(f"Reliability Diagram")
  ax.legend(loc=4, facecolor='white')
  return ax



class ModelKey(NamedTuple):
  model_type: str
  k: int  # number of members in the ensemble
  is_deterministic: bool
  tuning_domain: str
  num_mc_samples: int  # format f'mc{num_mc_samples}/'


RESULT_DICT = Dict[str, List[np.ndarray]]
MODEL_RESULT_DICT = Dict[str, RESULT_DICT]
DATA_RESULT_DICT = Dict[str, MODEL_RESULT_DICT]


def grid_plot_wrapper(fn,
                      n_plots,
                      ncols,
                      nrows=None,
                      get_args: Union[List[List], Callable[[int], List]]=None,
                      get_kwargs: Union[List[Dict], Callable[[int], Dict]]=None):
  if not get_args:
    get_args = lambda i: []
  if not get_kwargs:
    get_kwargs = lambda i: {}
  if isinstance(get_args, list):
    get_args = lambda i: get_args[i]
  if isinstance(get_kwargs, list):
    get_kwargs = lambda i: get_kwargs[i]
  if not nrows:
    nrows = int(np.ceil(n_plots / ncols))
  fig, axes = plt.subplots(nrows=nrows, ncols=ncols)
  flatten_axes = [a for axs in axes for a in axs]
  for i, ax in enumerate(flatten_axes):
    fn(*get_args(i), ax=ax, **get_kwargs(i))
  return fig


def parse_model_key(model_key: Tuple):
  model_type, k, is_deterministic, tuning_domain, num_mc_samples = model_key
  return ModelKey(model_type=model_type,
                  k=k,
                  is_deterministic=is_deterministic,
                  tuning_domain=tuning_domain,
                  num_mc_samples=int(num_mc_samples))


def plot_ece_all_methods(models_results_dict: MODEL_RESULT_DICT):
  return grid_plot_wrapper(
    fn=plot_ece_one_method,
    n_plots=len(models_results_dict),
    ncols=3,
    get_args=[[k, v] for k, v in models_results_dict.items()],
  )


def plot_ece_one_method(model_key, result_dict, ax=None):
  if not ax:
    plt.figure()
    ax = plt.gca()
  # compute mean and std ece curves for all seeds
  # plot them on an axes
  raise NotImplementedError


def mean(inputs):
  """Be able to take the mean of an empty array without hitting NANs."""
  # pylint disable necessary for numpy and pandas
  if len(inputs) == 0:  # pylint: disable=g-explicit-length-test
    return 0
  else:
    return np.mean(inputs)


def one_hot_encode(labels, num_classes=None):
  """One hot encoder for turning a vector of labels into a OHE matrix."""
  if num_classes is None:
    num_classes = len(np.unique(labels))
  return np.eye(num_classes)[labels]


def binary_converter(probs):
  """Converts a binary probability vector into a matrix."""
  return np.array([[1 - p, p] for p in probs])


def verify_probability_shapes(probs):
  """Verify shapes of probs vectors and possibly stack 1D probs into 2D."""
  if probs.ndim == 2:
    num_classes = probs.shape[1]
    if num_classes == 1:
      probs = probs[:, 0]
      probs = binary_converter(probs)
      num_classes = 2
  elif probs.ndim == 1:
    # Cover binary case
    probs = binary_converter(probs)
    num_classes = 2
  else:
    raise ValueError('Probs must have 1 or 2 dimensions.')
  return probs, num_classes


def plot_predictive_entropy_histogram_all_methods(data_result_dict: DATA_RESULT_DICT):
  return grid_plot_wrapper(
    fn=plot_predictive_entropy_histogram_one_method,
    ncols=3,
    n_plots=len(data_result_dict["in_domain_test"]),
    get_kwargs=lambda i: data_result_dict,
  )


def plot_predictive_entropy_histogram_one_method(data_result_dict: DATA_RESULT_DICT, ax=None):
  if not ax:
    plt.figure()
    ax = plt.gca()
  # draw histogram for ID
  # draw histogram for OOD
  raise NotImplementedError


def line_hist(array, thresholds, ax=None, **kwargs):
  if not ax:
    plt.figure()
    ax = plt.gca()
  counts = []
  for i in range(len(thresholds) - 1):
    count = np.sum(np.logical_and(array >= thresholds[i], array < thresholds[i + 1]))
    counts.append(count)
  ax.plot(thresholds[:-1], np.array(counts), **kwargs)
  return ax


def hide_top_and_right_axis(ax):
  ax.spines['right'].set_visible(False)
  ax.spines['top'].set_visible(False)


def use_serif_font():
  plt.rc('font', family='serif')


def plot_predictive_entropy_line_histogram(
        y_total_uncert,
        start=-0.01, end=2.5, step_size=0.1, ax=None,
        color="blue",
        **kwargs):
  if not ax:
    plt.figure()
    ax = plt.gca()
  thresholds = np.arange(start, end, step_size)
  line_hist(y_total_uncert, thresholds, ax=ax, color=color, **kwargs)
  ax.set_xlabel("Entropy (nats)")
  ax.set_ylabel("# of Examples")
  # ax.legend(loc='upper left', facecolor='white', fontsize='small')
  return ax


def plot_total_versus_aleatoric_uncertainty_all_methods(data_result_dict: DATA_RESULT_DICT):

  def one_iter(domain: str, method_key: str):
    result_dict = data_result_dict[domain][method_key]
    plot_total_versus_aleatoric_uncertainty(
      y_true=result_dict["y_true"],
      y_pred=result_dict["y_pred"],
      y_total_uncert=result_dict["y_total_uncert"],
    )

  combinations = [[domain, model_key]
                  for domain, model_dict in data_result_dict.items()
                  for model_key, _ in model_dict.items()]

  return grid_plot_wrapper(
    fn=one_iter,
    n_plots=len(combinations),
    ncols=3,
    get_args=combinations,
  )



def plot_total_versus_aleatoric_uncertainty(y_true, y_pred, y_total_uncert,
                                            threshold=0.5, alpha=0.3):
  label_pred = np.array(y_pred > threshold, dtype=np.int)
  correct_index = np.nonzero(y_true == label_pred)[0]
  wrong_index = np.nonzero(y_true != label_pred)[0]

  alea_total_array = np.stack([y_pred, y_total_uncert]).T

  fig, axes = plt.subplots(1, 2, sharey=True)
  # axes[0].scatter(alea_total_array[correct_index, 0], alea_total_array[correct_index, 1], alpha=alpha)
  # axes[1].scatter(alea_total_array[wrong_index, 0], alea_total_array[wrong_index, 1], alpha=alpha)
  sns.kdeplot(x=alea_total_array[correct_index, 0], y=alea_total_array[correct_index, 1], fill=True, ax=axes[0],
              color="green", alpha=alpha)
  sns.kdeplot(x=alea_total_array[wrong_index, 0], y=alea_total_array[wrong_index, 1], fill=True, ax=axes[1],
              color="red", alpha=alpha)


def read_eval_folder(path, allow_pickle=True):
  """
  e.g. path = "gs://drd-fsvi-severity-results/2021-08-23-23-06-42/ood_validation/eval_results_80"
  """
  filenames = tf.io.gfile.listdir(path)
  d = {}
  for fn in filenames:
    p = os.path.join(path, fn)
    with tf.io.gfile.GFile(p, "rb") as f:
      d[fn[:-4]] = np.load(f, allow_pickle=allow_pickle)
  return d
