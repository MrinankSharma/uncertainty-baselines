import json
import logging
import os
os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "true"
os.environ["XLA_PYTHON_CLIENT_MEM_FRACTION"] = "0.95"
import pdb
import pickle
import time
import types
from functools import partial

import haiku as hk
import jax
import optax
import tree
import numpy as np
from absl import app, flags
from jax import jit
from jax import random
tf_cpu_only = True  # TODO: check how this affects determinism -- keep set to False
if tf_cpu_only:
    import tensorflow as tf
    tf.config.experimental.set_visible_devices([], "GPU")
    print('WARNING: TensorFlow is set to only use CPU.')
from tqdm import tqdm
import jax.numpy as jnp
from tensorboard.plugins.hparams import api as hp

abspath = os.path.abspath(__file__)
dname = os.path.dirname(abspath)
path = dname + "/../.."
# print(f'Setting working directory to {path}\n')
os.chdir(path)

from baselines.diabetic_retinopathy_detection.fsvi_utils.utils import (
    initialize_random_keys,
    to_one_hot,
)
from baselines.diabetic_retinopathy_detection.fsvi_utils.utils_training import Training
from baselines.diabetic_retinopathy_detection.old_utils import get_diabetic_retinopathy_base_metrics, \
    get_diabetic_retinopathy_cpu_metrics
from baselines.diabetic_retinopathy_detection import utils

# original flags
flags.DEFINE_string(
    "data_training",
    "not_specified",
    "Training and in-distribution dataset used (default: not_specified)\n"
    "Examples: 'continual_learning_pmnist', 'continual_learning_smnist', "
    "'continual_learning_sfashionmnist'",
)


flags.DEFINE_string(
    "model_type",
    "not_specified",
    "Model used (default: not_specified). Example: 'fsvi_mlp', 'mfvi_cnn'",
)

flags.DEFINE_string("optimizer", "adam", "Optimizer used (default: adam)")

flags.DEFINE_string(
    "architecture", "not_specified", "Architecture of NN (default: not_specified)",
)

flags.DEFINE_string(
    "activation",
    "not_specified",
    "Activation function used in NN (default: not_specified)",
)

flags.DEFINE_string("prior_mean", "0", "Prior mean function (default: 0)")

flags.DEFINE_string("prior_cov", "0", help="Prior cov function (default: 0)")

flags.DEFINE_string(
    "prior_type",
    default="not_specified",
    help="Type of prior (default: not_specified)",
)

flags.DEFINE_integer(
    "epochs", default=100, help="Number of epochs for each task (default: 100)",
)

flags.DEFINE_float(
    "learning_rate", default=1e-3, help="Learning rate (default: 1e-3)",
)

flags.DEFINE_float("dropout_rate", default=0.0, help="Dropout rate (default: 0.0)")

flags.DEFINE_float(
    "regularization", default=0, help="Regularization parameter (default: 0)",
)

flags.DEFINE_integer(
    "n_inducing_inputs", default=0, help="Number of BNN inducing points (default: 0)",
)

flags.DEFINE_string(
    "inducing_input_type",
    default="not_specified",
    help="Inducing input selection method (default: not_specified)",
)

flags.DEFINE_string("kl_scale", default="1", help="KL scaling factor (default: 1)")

flags.DEFINE_boolean("full_cov", default=False, help="Use full covariance")

flags.DEFINE_integer(
    "n_samples", default=1, help="Number of exp log lik samples (default: 1)",
)

flags.DEFINE_float("tau", default=1.0, help="Likelihood precision (default: 1)")

flags.DEFINE_float("noise_std", default=1.0, help="Likelihood variance (default: 1)")

flags.DEFINE_list(
    "inducing_inputs_bound",
    default="-1.,1.",
    help="Inducing point range (default: [-1, 1])",
)

flags.DEFINE_integer(
    "logging_frequency",
    default=10,
    help="Logging frequency in number of epochs (default: 10)",
)

flags.DEFINE_list(
    "figsize", default="10,4", help="Size of figures (default: (10, 4))",
)

flags.DEFINE_integer("seed", default=0, help="Random seed (default: 0)")

flags.DEFINE_string(
    "save_path", default="debug", help="Path to save results (default: debug)",
)

flags.DEFINE_bool("save", default=False, help="Save output to file")

flags.DEFINE_bool("resume_training", default=False, help="Resume training")

flags.DEFINE_bool("map_initialization", default=False, help="MAP initialization")

flags.DEFINE_bool(
    "stochastic_linearization", default=False, help="Stochastic linearization"
)

# TODO: remove this option
flags.DEFINE_bool("batch_normalization", default=False, help="Batch normalization")

flags.DEFINE_bool("linear_model", default=False, help="Linear model")

flags.DEFINE_bool("features_fixed", default=False, help="Fixed feature maps")

flags.DEFINE_bool("debug", default=False, help="Debug model")

flags.DEFINE_string(
    "logroot",
    default=None,
    help="The root result folder that store runs for this type of experiment",
)

flags.DEFINE_string(
    "subdir",
    default=None,
    help="The subdirectory in logroot/runs/ corresponding to this run",
)

flags.DEFINE_integer('per_core_batch_size', 32,
                     'The per-core batch size for both training '
                     'and evaluation.')

# new flags copied from deterministic.py
flags.DEFINE_string(
    "output_dir",
    "/tmp/diabetic_retinopathy_detection/deterministic",
    "The directory where the model weights and training/evaluation summaries "
    "are stored. If you aim to use these as trained models for ensemble.py, "
    "you should specify an output_dir name that includes the random seed to "
    "avoid overwriting.",
)
flags.DEFINE_string("data_dir", None, "Path to training and testing data.")

flags.DEFINE_bool("use_validation", True, "Whether to use a validation split.")

flags.DEFINE_bool('use_test', True, 'Whether to use a test split.')
flags.DEFINE_string(
  'dr_decision_threshold', 'moderate',
  ("specifies where to binarize the labels {0, 1, 2, 3, 4} to create the "
   "binary classification task. Only affects the APTOS dataset partitioning. "
   "'mild': classify {0} vs {1, 2, 3, 4}, i.e., mild DR or worse?"
   "'moderate': classify {0, 1} vs {2, 3, 4}, i.e., moderate DR or worse?"))
# TODO: implementing load_from_checkpoint
flags.DEFINE_bool(
  'load_from_checkpoint', False, "Attempt to load from checkpoint")

flags.DEFINE_string(
    "class_reweight_mode",
    None,
    "Dataset is imbalanced (19.6%, 18.8%, 19.2% positive examples in train, val,"
    "test respectively). `None` (default) will not perform any loss reweighting. "
    "`constant` will use the train proportions to reweight the binary cross "
    "entropy loss. `minibatch` will use the proportions of each minibatch to "
    "reweight the loss.",
)

# General model flags.
# TODO: decide if we keep this
flags.DEFINE_integer(
    "checkpoint_interval",
    25,
    "Number of epochs between saving checkpoints. " "Use -1 to never save checkpoints.",
)

# Metric flags.
flags.DEFINE_integer("num_bins", 15, "Number of bins for ECE.")

# Learning rate / SGD flags.
flags.DEFINE_float("final_decay_factor", 1e-3, "How much to decay the LR by.")
flags.DEFINE_float("one_minus_momentum", 0.1, "Optimizer momentum.")
flags.DEFINE_string("lr_schedule", "step", "Type of LR schedule.")
flags.DEFINE_integer(
    "lr_warmup_epochs",
    1,
    "Number of epochs for a linear warmup to the initial "
    "learning rate. Use 0 to do no warmup.",
)
flags.DEFINE_float("lr_decay_ratio", 0.2, "Amount to decay learning rate.")
flags.DEFINE_list("lr_decay_epochs", ["30", "60"], "Epochs to decay learning rate by.")

# Accelerator flags.
flags.DEFINE_bool("force_use_cpu", False, "If True, force usage of CPU")
flags.DEFINE_bool("use_gpu", True, "Whether to run on GPU or otherwise TPU.")
flags.DEFINE_bool("use_bfloat16", False, "Whether to use mixed precision.")
flags.DEFINE_integer("num_cores", 1, "Number of TPU cores or number of GPUs.")
flags.DEFINE_string(
    "tpu",
    None,
    "Name of the TPU. Only used if force_use_cpu and use_gpu are both False.",
)
flags.DEFINE_integer(
    "n_samples_test", 1, "Number of MC samples used for validation and testing",
)
flags.DEFINE_float(
    "uniform_init_minval",
    -20.0,
    "lower bound of uniform distribution for variational log variance",
)
flags.DEFINE_float(
    "uniform_init_maxval",
    -18.0,
    "lower bound of uniform distribution for variational log variance",
)
flags.DEFINE_integer(
    "loss_type", 1, "type of loss",
)
flags.DEFINE_string(
    "w_init", "uniform", "initializer for weights",
)
flags.DEFINE_string(
    "b_init", "uniform", "initializer for bias",
)
flags.DEFINE_float('l2', 0.0, 'L2 regularization coefficient.')

# OOD flags.
flags.DEFINE_string(
  'distribution_shift', None,
  ("Specifies distribution shift to use, if any."
   "aptos: loads APTOS (India) OOD validation and test datasets. "
   "  Kaggle/EyePACS in-domain datasets are unchanged."
   "severity: uses DiabeticRetinopathySeverityShift dataset, a subdivision "
   "  of the Kaggle/EyePACS dataset to hold out clinical severity labels "
   "  as OOD."))
flags.DEFINE_bool(
  'load_train_split', True,
  "Should always be enabled - required to load train split of the dataset.")

FLAGS = flags.FLAGS


def process_args():
    """
    This is the only place where it is allowed to modify kwargs

    This function should not have side-effect.

    @param flags: input arguments
    @return:
    """
    # FLAGS doesn't accept renaming!
    FLAGS.figsize = (int(v) for v in FLAGS.figsize)
    FLAGS.inducing_inputs_bound = [float(v) for v in FLAGS.inducing_inputs_bound]


def get_dict_of_flags():
    return {k: getattr(FLAGS, k) for k in dir(FLAGS)}


def write_flags(path):
    d = get_dict_of_flags()
    string = json.dumps(d, indent=4, separators=(",", ":"))
    with tf.io.gfile.GFile(path, "w") as f:
        f.write(string)


def main(argv):
    del argv

    write_flags(os.path.join(FLAGS.output_dir, "flags.txt"))

    from jax.lib import xla_bridge

    print("*" * 100)
    print("Platform that is used by JAX:", xla_bridge.get_backend().platform)
    print("*" * 100)

    process_args()
    kh = initialize_random_keys(seed=FLAGS.seed)
    rng_key, rng_key_train, rng_key_test = random.split(kh.next_key(), 3)

    # LOAD DATA
    num_cores = FLAGS.num_cores

    per_core_batch_size = FLAGS.per_core_batch_size * num_cores

    datasets, steps = utils.load_dataset(
        train_batch_size=per_core_batch_size, eval_batch_size=per_core_batch_size,
        flags=FLAGS, strategy=None)
    available_splits = list(datasets.keys())
    test_splits = [split for split in available_splits if 'test' in split]
    eval_splits = [split for split in available_splits
                   if 'validation' in split or 'test' in split]
    eval_datasets = {split: datasets[split] for split in eval_splits}
    input_shape = [1] + utils.load_input_shape(dataset_train=datasets["train"])
    # TODO: remove this hardcoded value
    output_dim = 2
    # TODO: do we really need to keep all the inducing inputs selection methods?
    n_train = steps["train"] * per_core_batch_size
    dataset_train = datasets['train']
    train_steps_per_epoch = steps["train"]

    # Get the wrapper function which will produce uncertainty estimates for
    # our choice of method and Y/N ensembling.
    uncertainty_estimator_fn = utils.get_uncertainty_estimator(
        "fsvi", use_ensemble=False, use_tf=False)

    # INITIALIZE TRAINING CLASS
    training = Training(
        input_shape=input_shape,
        output_dim=output_dim,
        n_train=n_train,
        n_batches=train_steps_per_epoch,
        full_ntk=False,
        batch_size=per_core_batch_size,
        # TODO: is there a better way than this?
        **get_dict_of_flags(),
    )

    # INITIALIZE MODEL
    (model, _, apply_fn, state, params) = training.initialize_model(rng_key=rng_key)

    # INITIALIZE OPTIMIZATION
    (
        opt,
        opt_state,
        get_trainable_params,
        get_variational_and_model_params,
        objectives,
        loss,
        kl_evaluation,
        log_likelihood_evaluation,
        nll_grad_evaluation,
        task_evaluation,
        prediction_type,
    ) = training.initialize_optimization(
        model=model,
        apply_fn=apply_fn,
        params_init=params,
        state=state,
        rng_key=rng_key,
    )

    summary_writer = tf.summary.create_file_writer(
        os.path.join(FLAGS.output_dir, "summaries")
    )

    latest_checkpoint = get_latest_fsvi_checkpoint(FLAGS.output_dir)
    initial_epoch = 0
    if latest_checkpoint:
        with tf.io.gfile.GFile(latest_checkpoint, mode="rb") as f:
            chkpt = pickle.load(f)
        # TODO: need to validate the chkpt has compatible hyperparameters, such as
        # the type of the model, optimizer
        state, params, opt_state = chkpt["state"], chkpt["params"], chkpt["opt_state"]

        logging.info("Loaded checkpoint %s", latest_checkpoint)
        initial_epoch = chkpt["epoch"] + 1

    # INITIALIZE KL INPUT FUNCTIONS
    inducing_input_fn, prior_fn = training.kl_input_functions(
        apply_fn=apply_fn,
        predict_f_deterministic=model.predict_f_deterministic,
        state=state,
        params=params,
        prior_mean=FLAGS.prior_mean,
        prior_cov=FLAGS.prior_cov,
        rng_key=rng_key,
    )

    use_tpu = not (FLAGS.force_use_cpu or FLAGS.use_gpu)
    metrics = get_diabetic_retinopathy_base_metrics(
        use_tpu=use_tpu, num_bins=FLAGS.num_bins, use_validation=FLAGS.use_validation
    )
    # Define metrics outside the accelerator scope for CPU eval.
    # This will cause an error on TPU.
    if not use_tpu:
        metrics.update(
            get_diabetic_retinopathy_cpu_metrics(
                use_validation=FLAGS.use_validation
            )
        )
    metrics.update({"test/ms_per_example": tf.keras.metrics.Mean()})

    ########################## specify functions ##########################
    @jit
    def update_fn(
        params, state, opt_state, x_batch, y_batch, inducing_inputs, rng_key,
    ):
        """
        Captured variables:
            loss, FLAGS, num_cores
        """
        trainable_params, non_trainable_params = get_trainable_params(params)
        variational_params, model_params = get_variational_and_model_params(params)
        prior_mean, prior_cov = prior_fn(
            inducing_inputs=inducing_inputs, model_params=model_params,
        )

        grads, additional_info = jax.grad(loss, argnums=0, has_aux=True)(
            trainable_params,
            non_trainable_params,
            state,
            prior_mean,
            prior_cov,
            x_batch,
            y_batch,
            inducing_inputs,
            rng_key,
            FLAGS.class_reweight_mode == "constant",
            FLAGS.loss_type,
            FLAGS.l2,
        )

        zero_grads = jax.tree_map(lambda x: x * 0.0, non_trainable_params)
        grads = jax.tree_map(lambda x: x * 1.0, grads)
        grads_full = hk.data_structures.merge(grads, zero_grads)

        if num_cores > 1:
            grads_full = tree.map_structure(
                partial(jax.lax.pmean, axis_name="i"), grads_full
            )
            additional_info = tree.map_structure(
                partial(jax.lax.pmean, axis_name="i"), additional_info
            )
        updates, opt_state = opt.update(grads_full, opt_state)
        new_params = optax.apply_updates(params, updates)
        new_state = additional_info["state"]
        return new_params, new_state, opt_state, additional_info

    def parallelisable_train_per_batch_computation(
        params, state, opt_state, rng_key_train, x_batch, labels,
    ):
        """
        Captured variables:
            output_dim, FLAGS, update_fn, inducing_input_fn, model
        """
        # features has shape (batch_dim, 128, 128, 3), labels has shape (batch_dim,)
        y_batch = to_one_hot(labels, output_dim)
        inducing_key, rng_key_train, rng_key_eval = jax.random.split(rng_key_train, 3)
        inducing_inputs = inducing_input_fn(
            x_batch, inducing_key, FLAGS.n_inducing_inputs
        )
        params, state, opt_state, additional_info = update_fn(
            params, state, opt_state, x_batch, y_batch, inducing_inputs, rng_key_train,
        )
        # compute metrics
        _, probs, _ = model.predict_y_multisample(
            params=params,
            state=state,
            inputs=x_batch,
            rng_key=rng_key_eval,
            n_samples=1,
            is_training=False,
        )
        return params, state, opt_state, additional_info, probs

    if num_cores > 1:
        parallelisable_train_per_batch_computation = jax.pmap(
            parallelisable_train_per_batch_computation,
            axis_name="i",
            in_axes=(None, None, None, 0, 0, 0),
            out_axes=(None, None, None, None, 0),
        )

    ########################## train-eval loop ##########################

    verbose = False

    print(f"\n--- Training for {FLAGS.epochs} epochs ---\n")
    train_iterator = iter(dataset_train)
    for epoch in range(initial_epoch, FLAGS.epochs):
        t0 = time.time()
        # for _ in tqdm(range(train_steps_per_epoch), desc="gradient steps..."):
        #     if verbose:
        #         start = time.time()
        #     data = next(train_iterator)
        #     x_batch, labels = data["features"]._numpy(), data["labels"]._numpy()
        #     orig_labels = labels
        #     if verbose:
        #         print(f"loading data used {time.time() - start:.2f} seconds")
        #         start = time.time()
        #     _, rng_key_train = random.split(rng_key_train)
        #     if num_cores > 1:
        #         keys = random.split(rng_key_train, num_cores)
        #         x_batch, labels = reshape_to_multiple_cores(x_batch, labels, num_cores)
        #     else:
        #         keys = rng_key_train
        #     (
        #         params,
        #         state,
        #         opt_state,
        #         additional_info,
        #         probs,
        #     ) = parallelisable_train_per_batch_computation(
        #         params, state, opt_state, keys, x_batch, labels,
        #     )
        #     if verbose:
        #         print(f"per-batch computation used {time.time() - start:.2f} seconds")
        #         start = time.time()
        #
        #     if num_cores > 1:
        #         probs = reshape_to_one_core(probs)
        #
        #     if verbose:
        #         print(f"reshape again used {time.time() - start:.2f} seconds")
        #         start = time.time()
        #
        #     metrics["train/loss"].update_state(additional_info["loss"].item())
        #     log_likelihood_per_input = (
        #         additional_info["log_likelihood"].item() / orig_labels.shape[0]
        #     )
        #     metrics["train/negative_log_likelihood"].update_state(
        #         -log_likelihood_per_input
        #     )
        #     probs_of_labels = probs[:, 1]
        #     metrics["train/accuracy"].update_state(orig_labels, probs_of_labels)
        #     metrics["train/auprc"].update_state(orig_labels, probs_of_labels)
        #     metrics["train/auroc"].update_state(orig_labels, probs_of_labels)
        #
        #     if not use_tpu:
        #         metrics["train/ece"].add_batch(probs_of_labels, label=orig_labels)
        #     if verbose:
        #         print(f"compute metric used {time.time() - start:.2f} seconds")

        # Wrap our estimator to predict probabilities
        # the signature of the estimator is (inputs, training, num_samples, rng_key) -> np.ndarray
        # the required signature is (inputs, training)
        eval_estimator = wrap_fsvi_estimator(
            model=model, params=params, state=state,
            use_mixed_precision=FLAGS.use_bfloat16)
        _, rng_key_test = random.split(rng_key_test)
        estimator_args = {
            "rng_key": rng_key_test,
            "num_samples": FLAGS.n_samples_test
        }
        total_results = utils.evaluate_model_and_compute_metrics(
            None, eval_datasets, steps, metrics, eval_estimator,
            uncertainty_estimator_fn, per_core_batch_size, available_splits,
            estimator_args=estimator_args, is_deterministic=True, num_bins=FLAGS.num_bins,
            use_tpu=use_tpu, num_cores=num_cores, backend="jax")

        with summary_writer.as_default():
            for name, result in total_results.items():
                if result is not None:
                    tf.summary.scalar(name, result, step=epoch + 1)

        for metric in metrics.values():
            metric.reset_states()

        T0 = time.time()
        print(f"Epoch {epoch} used {T0 - t0:.2f} seconds")

        if (
            FLAGS.checkpoint_interval > 0
            and (epoch + 1) % FLAGS.checkpoint_interval == 0
        ):
            hparams = {
                k: v
                for k, v in get_dict_of_flags().items()
                if not isinstance(v, types.GeneratorType)
            }
            to_save = {
                "params": params,
                "state": state,
                # TODO: improve this way of saving hyperparameters
                # TODO: figure out why the figsize has type generator
                "hparams": hparams,
                "opt_state": opt_state,
                "epoch": epoch,
            }
            # Also save Keras model, due to checkpoint.save issue
            chkpt_name = os.path.join(FLAGS.output_dir, f"chkpt_{epoch + 1}")
            with tf.io.gfile.GFile(chkpt_name, mode="wb") as f:
                pickle.dump(to_save, f)
            logging.info("Saved checkpoint to %s", chkpt_name)

    to_save = {
        "params": params,
        "state": state,
        # TODO: improve this way of saving hyperparameters
        # TODO: figure out why the figsize has type generator
        "hparams": hparams,
        "opt_state": opt_state,
        "epoch": epoch,
    }
    final_checkpoint_name = os.path.join(FLAGS.output_dir, "final_checkpoint")
    with tf.io.gfile.GFile(final_checkpoint_name, mode="wb") as f:
        pickle.dump(to_save, f)
    logging.info("Saved last checkpoint to %s", final_checkpoint_name)


    with summary_writer.as_default():
        hp.hparams(
            {
                "learning_rate": FLAGS.learning_rate,
                'per_core_batch_size': FLAGS.per_core_batch_size,
                'final_decay_factor': FLAGS.final_decay_factor,
                'one_minus_momentum': FLAGS.one_minus_momentum,
                'optimizer': FLAGS.optimizer,
                'loss_type': FLAGS.loss_type,
                'l2': FLAGS.l2
            }
        )


def wrap_fsvi_estimator(model, params, state, use_mixed_precision):

    def estimator_wrapper(inputs, training, num_samples, rng_key):
        preds_y_samples, _, _ = model.predict_y_multisample_jitted(
            params=params,
            state=state,
            inputs=inputs,
            rng_key=rng_key,
            n_samples=num_samples,
            is_training=training,
        )
        if use_mixed_precision:
            preds_y_samples = preds_y_samples.astype(jnp.float32)
        probs = preds_y_samples[:, :, 1]
        return probs

    return estimator_wrapper


@jax.jit
def reshape_to_one_core(x):
    return jnp.reshape(x, [-1] + list(x.shape[2:]))


@partial(jax.jit, static_argnums=(2,))
def reshape_to_multiple_cores(x_batch, y_batch, num_cores):
    func = lambda x: x.reshape([num_cores, -1] + list(x.shape)[1:])
    x_batch, y_batch = list(map(func, [x_batch, y_batch],))
    return x_batch, y_batch


def get_latest_fsvi_checkpoint(path):
    chkpts = [f for f in tf.io.gfile.listdir(path) if "chkpt_" in f]
    if chkpts:
        latest = max(chkpts, key=lambda x: int(x.split("_")[1]))
        return os.path.join(path, latest)


if __name__ == "__main__":
    app.run(main)
