# -*- coding: utf-8 -*-
"""
This module defines a general procedure for running applications.

Example usage::
    app_driver = ApplicationDriver()
    app_driver.initialise_application(system_param, input_data_param)
    app_driver.run_application()

``system_param`` and ``input_data_param`` should be generated using:
``niftynet.utilities.user_parameters_parser.run()``
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time

import tensorflow as tf

from niftynet.engine.application_factory import \
    ApplicationFactory, EventHandlerFactory, IteratorFactory
from niftynet.engine.application_iteration import IterationMessage
from niftynet.engine.application_variables import \
    GradientsCollector, OutputsCollector
from niftynet.engine.signal import TRAIN, \
    ITER_STARTED, ITER_FINISHED, SESS_STARTED, SESS_FINISHED
from niftynet.io.image_sets_partitioner import ImageSetsPartitioner
from niftynet.io.misc_io import \
    get_latest_subfolder, touch_folder, infer_latest_model_file
from niftynet.layer.bn import BN_COLLECTION
from niftynet.utilities.util_common import \
    set_cuda_device, tf_config, device_string
from niftynet.utilities.user_parameters_default import \
    DEFAULT_EVENT_HANDLERS, DEFAULT_ITERATION_GENERATOR


# pylint: disable=too-many-instance-attributes
class ApplicationDriver(object):
    """
    This class initialises an application by building a TF graph,
    and maintaining a session and coordinator. It controls the
    starting/stopping of an application. Applications should be
    implemented by inheriting ``niftynet.application.base_application``
    to be compatible with this driver.
    """

    def __init__(self):
        self.app = None
        self.graph = tf.Graph()

        self.is_training_action = True
        self.num_threads = 0
        self.num_gpus = 0

        self.model_dir = None
        self.summary_dir = None
        self.max_checkpoints = 2
        self.save_every_n = 0
        self.tensorboard_every_n = -1

        self.initial_iter = 0
        self.final_iter = 0
        self.validation_every_n = -1
        self.validation_max_iter = 1

        self.coordinator = tf.train.Coordinator()
        self.data_partitioner = None
        self.outputs_collector = None
        self.gradients_collector = None

        self.event_handler_names = None
        self.iterator_type = None
        self._event_handlers = []

    def initialise_application(self, workflow_param, data_param):
        """
        This function receives all parameters from user config file,
        create an instance of application.

        :param workflow_param: a dictionary of user parameters,
            keys correspond to sections in the config file
        :param data_param: a dictionary of input image parameters,
            keys correspond to data properties to be used by image_reader
        :return:
        """
        try:
            system_param = workflow_param.get('SYSTEM', None)
            net_param = workflow_param.get('NETWORK', None)
            train_param = workflow_param.get('TRAINING', None)
            infer_param = workflow_param.get('INFERENCE', None)
            app_param = workflow_param.get('CUSTOM', None)
        except AttributeError:
            tf.logging.fatal('parameters should be dictionaries')
            raise
        self.event_handler_names = \
            system_param.event_handler or DEFAULT_EVENT_HANDLERS
        self.iterator_type = \
            system_param.iteration_generator or DEFAULT_ITERATION_GENERATOR

        assert os.path.exists(system_param.model_dir), \
            'Model folder not exists {}'.format(system_param.model_dir)
        self.is_training_action = TRAIN.startswith(system_param.action.lower())
        # hardware-related parameters
        self.num_threads = max(system_param.num_threads, 1) \
            if self.is_training_action else 1
        self.num_gpus = system_param.num_gpus \
            if self.is_training_action else min(system_param.num_gpus, 1)
        set_cuda_device(system_param.cuda_devices)

        # set output TF model folders
        self.model_dir = touch_folder(
            os.path.join(system_param.model_dir, 'models'))

        # set training params.
        if self.is_training_action:
            assert train_param, 'training parameters not specified'
            self.initial_iter = train_param.starting_iter
            # creating new summary subfolder if it's not finetuning
            summary_root = os.path.join(system_param.model_dir, 'logs')
            self.summary_dir = get_latest_subfolder(
                summary_root, create_new=train_param.starting_iter == 0)

            self.final_iter = max(train_param.max_iter, self.initial_iter)
            self.save_every_n = train_param.save_every_n
            self.tensorboard_every_n = train_param.tensorboard_every_n
            self.max_checkpoints = \
                max(train_param.max_checkpoints, self.max_checkpoints)
            self.gradients_collector = GradientsCollector(
                n_devices=max(self.num_gpus, 1))
            self.validation_every_n = train_param.validation_every_n
            if self.validation_every_n > 0:
                self.validation_max_iter = max(self.validation_max_iter,
                                               train_param.validation_max_iter)
            action_param = train_param
        else:  # set inference params.
            assert infer_param, 'inference parameters not specified'
            self.initial_iter = infer_param.inference_iter
            action_param = infer_param

        # infer the initial iteration from model files
        if self.initial_iter < 0:
            self.initial_iter = infer_latest_model_file(self.model_dir)
        self.outputs_collector = OutputsCollector(
            n_devices=max(self.num_gpus, 1))

        # create an application instance
        assert app_param, 'application specific param. not specified'
        app_module = ApplicationDriver._create_app(app_param.name)
        self.app = app_module(net_param, action_param, system_param.action)

        # initialise data input
        self.data_partitioner = ImageSetsPartitioner()
        # clear the cached file lists
        self.data_partitioner.reset()

        if data_param:
            do_new_partition = \
                self.is_training_action and self.initial_iter == 0 and \
                (not os.path.isfile(system_param.dataset_split_file)) and \
                (train_param.exclude_fraction_for_validation > 0 or
                 train_param.exclude_fraction_for_inference > 0)
            data_fractions = (train_param.exclude_fraction_for_validation,
                              train_param.exclude_fraction_for_inference) \
                if do_new_partition else None
            self.data_partitioner.initialise(
                data_param=data_param,
                new_partition=do_new_partition,
                ratios=data_fractions,
                data_split_file=system_param.dataset_split_file)
            assert self.data_partitioner.has_validation or \
                self.validation_every_n <= 0, \
                'validation_every_n is set to {}, ' \
                'but train/validation splitting not available.\nPlease ' \
                'check dataset partition list {} ' \
                '(remove file to generate a new dataset partition), ' \
                'check "exclude_fraction_for_validation" ' \
                '(current config value: {}).\nAlternatively, ' \
                'set "validation_every_n" to -1.'.format(
                    self.validation_every_n,
                    system_param.dataset_split_file,
                    train_param.exclude_fraction_for_validation)

        # initialise readers
        self.app.initialise_dataset_loader(
            data_param, app_param, self.data_partitioner)

        # pylint: disable=not-context-manager
        with self.graph.as_default(), tf.name_scope('Sampler'):
            self.app.initialise_sampler()

    def load_event_handlers(self, names):
        """
        Import event handler modules and create a list of handler instances.
        The event handler instances will be stored with this engine.

        :param names: strings of event handlers
        :return:
        """
        self._event_handlers = []
        for name in set(names):
            the_event_class = EventHandlerFactory.create(name)
            # initialise all registered event handler classes
            engine_config_dict = vars(self)
            self._event_handlers.append(the_event_class(**engine_config_dict))

    def run_application(self):
        """
        Initialise a TF graph, connect data sampler and network within
        the graph context, run training loops or inference loops.

        :return:
        """
        with tf.Session(config=tf_config(), graph=self.graph):

            self.graph = self.create_graph(self.graph)

            # import the generator class
            generator = ApplicationDriver._create_iters(self.iterator_type)

            # check app variables initialised and ready for starts
            # self.app.check_initialisations()

            # make the list of initialised event handler instances.
            self.load_event_handlers(self.event_handler_names)

            start_time = time.time()
            loop_status = {
                'current_iter': self.initial_iter, 'normal_exit': False}
            try:
                # create a iteration message generator and
                # iteratively run the graph (the main engine loop)
                engine_config_dict = vars(self)
                self.loop(application=self.app,
                          iteration_messages=generator(**engine_config_dict)(),
                          loop_status=loop_status,
                          coordinator=self.coordinator)

            except KeyboardInterrupt:
                tf.logging.warning('User cancelled application')
            except tf.errors.OutOfRangeError:
                if not loop_status.get('normal_exit', False):
                    # reached the end of inference Dataset
                    loop_status['normal_exit'] = True
            except RuntimeError:
                import sys
                import traceback
                exc_type, exc_value, exc_traceback = sys.exc_info()
                traceback.print_exception(
                    exc_type, exc_value, exc_traceback, file=sys.stdout)
            finally:
                tf.logging.info('cleaning up...')
                if not loop_status.get('normal_exit', False):
                    # loop didn't finish normally,
                    # again broadcasting session finished event
                    iter_msg = IterationMessage()
                    iter_msg.current_iter = loop_status.get('current_iter', -1)
                    SESS_FINISHED.send(self.app, iter_msg=iter_msg)
                    tf.logging.warning('stopped early, incomplete iterations.')
                self.app.stop()
                tf.logging.info(
                    "%s stopped (time in second %.2f).",
                    type(self.app).__name__, (time.time() - start_time))

    # pylint: disable=not-context-manager
    def create_graph(self, graph=tf.Graph()):
        """
        TensorFlow graph is only created within this function.
        """
        assert isinstance(graph, tf.Graph)
        main_device = device_string(
            self.num_gpus, 0,
            is_worker=False, is_training=self.is_training_action)
        # start constructing the graph, handling training and inference cases
        with graph.as_default(), tf.device(main_device):

            # initialise network, these are connected in
            # the context of multiple gpus
            self.app.initialise_network()
            self.app.add_validation_flag()

            # for data parallelism --
            #     defining and collecting variables from multiple devices
            bn_ops = None
            for gpu_id in range(0, max(self.num_gpus, 1)):
                worker_device = device_string(
                    self.num_gpus, gpu_id,
                    is_worker=True, is_training=self.is_training_action)
                scope_string = 'worker_{}'.format(gpu_id)
                with tf.name_scope(scope_string) as scope:
                    with tf.device(worker_device):
                        # setup network for each of the multiple devices
                        self.app.connect_data_and_network(
                            self.outputs_collector,
                            self.gradients_collector)
                        if self.is_training_action:
                            # batch norm statistics from the last device
                            bn_ops = tf.get_collection(BN_COLLECTION, scope)

            # assemble all training operations
            if self.is_training_action and self.gradients_collector:
                updates_op = []
                # batch normalisation moving averages operation
                if bn_ops:
                    updates_op.extend(bn_ops)
                # combine them with model parameter updating operation
                with tf.name_scope('ApplyGradients'):
                    with graph.control_dependencies(updates_op):
                        self.app.set_network_gradient_op(
                            self.gradients_collector.gradients)

            # # initialisation operation
            # with tf.name_scope('Initialization'):
            #     self.init_op = global_vars_init_or_restore()

            with tf.name_scope('MergedOutputs'):
                self.outputs_collector.finalise_output_op()

        # tf.Graph.finalize(graph)
        return graph

    @staticmethod
    def loop(application,
             iteration_messages=(),
             loop_status=None,
             coordinator=None):
        """
        Running ``loop_step`` with ``IterationMessage`` instances
        generated by ``iteration_generator``.

        This loop stops when any of the condition satisfied:
            1. no more element from the ``iteration_generator``;
            2. ``application.interpret_output`` returns False;
            3. any exception raised.
            4. coordinator requests stopping.
        Broadcasting SESS_* signals at the beginning and end of this method.

        This function should be used in a context of
        ``tf.Session`` or ``session.as_default()``.

        :param application: a niftynet.application instance, application
            will provides ``tensors`` to be fetched by ``tf.session.run()``.
        :param iteration_messages:
            a generator of ``engine.IterationMessage`` instances
        :param loop_status: dictionary used to capture the loop status,
            useful when the loop exited in an unexpected manner.
        :param coordinator: thread coordinator used to stop the loop.
        :return:
        """
        # broadcasting event of session started
        SESS_STARTED.send(application, iter_msg=IterationMessage())

        loop_status = loop_status or {}
        for iter_msg in iteration_messages:
            if coordinator and coordinator.should_stop():
                break
            loop_status['current_iter'] = iter_msg.current_iter

            # run an iteration
            ApplicationDriver.loop_step(application, iter_msg)

            # Checking stopping conditions
            if iter_msg.should_stop:
                tf.logging.info('stopping -- event handler: %s.',
                                iter_msg.should_stop)
                break

        loop_status['normal_exit'] = True
        # broadcasting event of session finished
        iter_msg = IterationMessage()
        iter_msg.current_iter = loop_status.get('current_iter', -1)
        SESS_FINISHED.send(application, iter_msg=iter_msg)

    @staticmethod
    def loop_step(app, iteration_message):
        """
        Calling ``tf.session.run`` with parameters encapsulated in
        iteration message as an iteration.
        Broadcasting ITER_* events before and afterward.

        :param app:
        :param iteration_message: an ``engine.IterationMessage`` instances
        :return:
        """
        # broadcasting event of starting an iteration
        ITER_STARTED.send(app, iter_msg=iteration_message)

        # ``iter_msg.ops_to_run`` are populated with the ops to run in
        # each iteration, fed into ``session.run()`` and then
        # passed to the app (and observers) for interpretation.
        sess = tf.get_default_session()
        # make sure this
        assert sess, 'method should be called within a TF session context.'
        iteration_message.current_iter_output = sess.run(
            iteration_message.ops_to_run,
            feed_dict=iteration_message.data_feed_dict)

        # broadcasting event of finishing an iteration
        ITER_FINISHED.send(app, iter_msg=iteration_message)

    @staticmethod
    def _create_app(app_type_string):
        """
        Import the application module
        """
        return ApplicationFactory.create(app_type_string)

    @staticmethod
    def _create_iters(iterator_string):
        """
        Import the Iterator module (used in the main training/infer loop).
        """
        return IteratorFactory.create(iterator_string)
