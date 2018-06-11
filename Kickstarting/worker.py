#!/usr/bin/env python
import cv2
import go_vncdriver
import tensorflow as tf
import argparse
import logging
import sys, signal
import time
import os
from a3c import A3C
from envs import create_env
import distutils.version
use_tf12_api = distutils.version.LooseVersion(tf.VERSION) >= distutils.version.LooseVersion('0.12.0')

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Disables write_meta_graph argument, which freezes entire process and is mostly useless.
class FastSaver(tf.train.Saver):
    def save(self, sess, save_path, global_step=None, latest_filename=None,
             meta_graph_suffix="meta", write_meta_graph=True):
        super(FastSaver, self).save(sess, save_path, global_step, latest_filename,
                                    meta_graph_suffix, False)

def run(args, server):
    env = create_env(args.env_id, client_id=str(args.task), remotes=args.remotes)
    if args.teacher:
        teacher = A3C(env, args.task, args.visualise)
        trainer = A3C(env, args.task, args.visualise, teacher= teacher, name="student")
    else:
        teacher = None
        trainer = A3C(env, args.task, args.visualise, teacher= teacher)

    # Variable names that start with "local" are not saved in checkpoints.
    if use_tf12_api:
        variables_to_save = [v for v in tf.global_variables() if not v.name.startswith("local") and "teacher" not in v.name]
        all_student_variables = [v for v in tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES) if "student" in v.name]
        init_op = tf.variables_initializer(variables_to_save)
        init_all_op = tf.variables_initializer(all_student_variables)
    else:
        variables_to_save = [v for v in tf.global_variables() if not v.name.startswith("local") and "teacher" not in v.name]
        init_op = tf.initialize_variables(variables_to_save)
        all_student_variables = [v for v in tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES) if "student" in v.name]
        init_all_op = tf.variables_initializer(all_student_variables)

    saver = FastSaver(variables_to_save)

    var_list = all_student_variables #tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, tf.get_variable_scope().name)

    logger.info('Trainable vars:')
    for v in var_list:
        logger.info('  %s %s', v.name, v.get_shape())

    def init_fn(ses):
        logger.info("Initializing all parameters.")
        ses.run(init_all_op)

    def get_init_fn():

        if args.checkpoint_path is None and teacher is None:
            return lambda sess: init_fn(sess)

        # Warn the user if a checkpoint exists in the train_dir. Then we'll be
        # ignoring the checkpoint anyway.
        #train_dir = os.path.join(args.log_dir, 'train')
        #if tf.train.latest_checkpoint(train_dir):
        #    logger.info('Ignoring --checkpoint_path because a checkpoint already exists in %s'% train_dir)
        #    return lambda sess: init_fn(sess)

        if args.teacher:
            teacher_variables = [v for v in tf.global_variables() if "global/teacher" in v.name]
            exclusions = []
            if args.checkpoint_exclude_scopes:
                    exclusions = [scope.strip() for scope in FLAGS.checkpoint_exclude_scopes.split(',')]

            variables_to_restore = []

            for var in teacher_variables: #tf.contrib.framework.get_model_variables():
                for exclusion in exclusions:
                    if var.op.name.startswith(exclusion):
                        break
                else:
                    variables_to_restore.append(tf.Variable(var, name=var.name.replace("teacher/","")))

            if tf.gfile.IsDirectory(args.checkpoint_path):
                checkpoint_path = tf.train.latest_checkpoint(args.checkpoint_path)
            else:
                checkpoint_path = args.checkpoint_path

            logger.info('Fine-tuning from %s' % checkpoint_path)
            print(variables_to_restore)

            return tf.contrib.framework.assign_from_checkpoint_fn(checkpoint_path,
                                              variables_to_restore,
                                              ignore_missing_vars=args.ignore_missing_vars)

    config = tf.ConfigProto(device_filters=["/job:ps", "/job:worker/task:{}/cpu:0".format(args.task)])
    logdir = os.path.join(args.log_dir, 'train')

    if use_tf12_api:
        summary_writer = tf.summary.FileWriter(logdir + "_%d" % args.task)
    else:
        summary_writer = tf.train.SummaryWriter(logdir + "_%d" % args.task)

    logger.info("Events directory: %s_%s", logdir, args.task)
    sv = tf.train.Supervisor(is_chief=(args.task == 0),
                             logdir=logdir,
                             saver=saver,
                             summary_op=None,
                             init_op=init_op,
                             init_fn=get_init_fn(),
                             summary_writer=summary_writer,
                             ready_op=tf.report_uninitialized_variables(variables_to_save),
                             global_step=trainer.global_step,
                             save_model_secs=30,
                             save_summaries_secs=30)

    num_global_steps = 100000000

    logger.info(
        "Starting session. If this hangs, we're mostly likely waiting to connect to the parameter server. " +
        "One common cause is that the parameter server DNS name isn't resolving yet, or is misspecified.")
    with sv.managed_session(server.target, config=config) as sess, sess.as_default():
        sess.run(trainer.sync)
        trainer.start(sess, summary_writer)
        global_step = sess.run(trainer.global_step)
        logger.info("Starting training at step=%d", global_step)
        while not sv.should_stop() and (not num_global_steps or global_step < num_global_steps):
            trainer.process(sess)
            global_step = sess.run(trainer.global_step)

    # Ask for all the services to stop.
    sv.stop()
    logger.info('reached %s steps. worker stopped.', global_step)

def cluster_spec(num_workers, num_ps):
    """
More tensorflow setup for data parallelism
"""
    cluster = {}
    port = 12222

    all_ps = []
    host = '127.0.0.1'
    for _ in range(num_ps):
        all_ps.append('{}:{}'.format(host, port))
        port += 1
    cluster['ps'] = all_ps

    all_workers = []
    for _ in range(num_workers):
        all_workers.append('{}:{}'.format(host, port))
        port += 1
    cluster['worker'] = all_workers
    return cluster

def main(_):
    """
Setting up Tensorflow for data parallel work
"""

    parser = argparse.ArgumentParser(description=None)
    parser.add_argument('-v', '--verbose', action='count', dest='verbosity', default=0, help='Set verbosity.')
    parser.add_argument('--task', default=0, type=int, help='Task index')
    parser.add_argument('--job-name', default="worker", help='worker or ps')
    parser.add_argument('--num-workers', default=1, type=int, help='Number of workers')
    parser.add_argument('--log-dir', default="/tmp/pong", help='Log directory path')
    parser.add_argument('--teacher', action='store_true',
                help="Whether or not to kickstarting with a teacher")

    parser.add_argument('--checkpoint_path', help='A path to a checkpoint from which to finetune')
    parser.add_argument('--checkpoint_exclude_scopes', help='Comma-separated list of scopes of variables to exclude when restoring from a checkpoint')
    parser.add_argument('--ignore_missing_vars', action='store_true',
                    help="When restoring from checkpoint would ignore missing variables")
    parser.add_argument('--env-id', default="PongDeterministic-v3", help='Environment id')
    parser.add_argument('-r', '--remotes', default=None,
                        help='References to environments to create (e.g. -r 20), '
                             'or the address of pre-existing VNC servers and '
                             'rewarders to use (e.g. -r vnc://localhost:5900+15900,vnc://localhost:5901+15901)')

    # Add visualisation argument
    parser.add_argument('--visualise', action='store_true',
                        help="Visualise the gym environment by running env.render() between each timestep")

    args = parser.parse_args()
    spec = cluster_spec(args.num_workers, 1)
    cluster = tf.train.ClusterSpec(spec).as_cluster_def()

    def shutdown(signal, frame):
        logger.warn('Received signal %s: exiting', signal)
        sys.exit(128+signal)
    signal.signal(signal.SIGHUP, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if args.job_name == "worker":
        server = tf.train.Server(cluster, job_name="worker", task_index=args.task,
                                 config=tf.ConfigProto(intra_op_parallelism_threads=1, inter_op_parallelism_threads=2))
        run(args, server)
    else:
        server = tf.train.Server(cluster, job_name="ps", task_index=args.task,
                                 config=tf.ConfigProto(device_filters=["/job:ps"]))
        while True:
            time.sleep(1000)

if __name__ == "__main__":
    tf.app.run()
