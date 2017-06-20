import glob
import numpy as np
import os
import scipy.special
import time

# import abr_control.utils.os_utils
from abr_control.utils.paths import cache_dir
from .signal import Signal

try:
    import nengo
except ImportError:
    raise Exception('Nengo module needs to be installed to ' +
                    'use adaptive dynamics.')

nengolib = None
try:
    import nengolib
except ImportError:
    print('Nengo lib not installed, encoder placement will be sub-optimal.')


class DynamicsAdaptation(Signal):
    """ An implementation of nonlinear dynamics adaptation using Nengo,
    as described in (DeWolf, Stewart, Slotine, and Eliasmith, 2016)

    The model learns to account for unknown / unmodelled external or
    internal forces, using an efferent copy of the control signal to train.

    Parameters
    ----------
    robot_config : class instance
        contains all relevant information about the arm
        such as: number of joints, number of links, mass information etc.
    n_neurons : int, optional (Default: 1000)
        number of neurons per adaptive population
    n_adapt_pop : int, optional (Default: 1)
        number of adaptive populations
    pes_learning_rate : float, optional (Default: 1e-6)
        controls the speed of neural adaptation
    intercepts list : list of two floats, optional (Default: (0.5, 1.0))
        voltage threshold range for neurons
    spiking : boolean, optional (Default: False)
        use spiking or rate mode neurons
    filter_error : boolean, optional (Default: False)
        apply low pass filter to the error signal or not
    weights_file : string, optional (Default: None)
        path to file where learned weights are saved
    backend : string, optional (Default: nengo)
        {'nengo', 'nengo_ocl', 'nengo_spinnaker'}
    use_dq : boolean, optional (Default: False)
        set True to pass in position and velocity,
        False if only passing in position
    trial: int, optional (Default: None)
        if doing multiple trials of n runs to average over.
        if set to None it will search for the most recent trial
        to save to, saving to 'trial0' if none exist
    run: int, optional (Default: None)
        the current run number. This value will automatically
        increment based on the last run saved in the test_name
        folder. The user can specify a run if they desire to
        overwrite a previous run. If set to None then the test_name
        folder will be searched for the next run to save as
    test_name: string, optional (Default: 'test')
        the test name to save the weights under
    autoload: boolean, optional (Default: False)
        set true if you would like the script to check the 'test_name'
        directory for the most recent saved weights, False to start
        with a zeros array of weights, or a specified 'weights_file'
    """

    def __init__(self, robot_config,
                 n_neurons=1000,
                 n_adapt_pop=1,
                 pes_learning_rate=1e-6,
                 intercepts=(0.5, 1.0),
                 spiking=False,
                 filter_error=False,
                 weights_file=None,
                 backend='nengo',
                 use_dq=False,
                 trial=None,
                 run=None,
                 test_name='test',
                 autoload=False):
        self.robot_config = robot_config
        self.u_adapt = np.zeros(self.robot_config.N_JOINTS)
        self.use_dq = use_dq
        self.n_adapt_pop = n_adapt_pop

        if self.use_dq is True:
            self.N_INPUTS = 2
        else:
            self.N_INPUTS = 1

        # if a weights_file is not specified and auto_load is desired,
        # check the test_name directory for the most recent weights
        if weights_file is None and not autoload:
            weights_file = ['']*self.n_adapt_pop
        elif weights_file is None and autoload:
            weights_file = self.load_weights(trial=trial,run=run, test_name=test_name)

        dim = self.robot_config.N_JOINTS
        nengo_model = nengo.Network(seed=10)
        with nengo_model:

            # create a Node for passing in the scaled system feedback
            def qdq_input(t):
                """ returns q and dq """

                q = ((self.q + np.pi) % (np.pi*2)) - np.pi

                if self.use_dq is True:
                    output = (np.hstack([
                              self.robot_config.scaledown('q', q),
                              self.robot_config.scaledown('dq', self.dq)]))
                else:
                    output = self.robot_config.scaledown('q', q)

                return output
            qdq_input = nengo.Node(qdq_input, size_out=dim * self.N_INPUTS)

            # create a Node for passing in the training signal
            def u_input(t):
                """ returns the control signal for training """

                return self.training_signal
            u_input = nengo.Node(u_input, size_out=dim)

            # create a Node for saving the adaptive population output
            def u_adapt_output(t, x):
                """ stores the adaptive output for use in control() """

                self.u_adapt = np.copy(x)
            output = nengo.Node(u_adapt_output, size_in=dim, size_out=0)

            self.adapt_ens = []
            conn_learn = []
            for ii in range(self.n_adapt_pop):
                N_DIMS = self.robot_config.N_JOINTS * self.N_INPUTS
                intercepts = AreaIntercepts(
                    dimensions=N_DIMS,
                    base=nengo.dists.Uniform(intercepts[0], intercepts[1]))

                if spiking:
                    neuron_type = nengo.LIF()
                else:
                    neuron_type = nengo.LIFRate()

                # create the adaptive ensembles
                self.adapt_ens.append(nengo.Ensemble(
                    n_neurons=n_neurons,
                    dimensions=N_DIMS,
                    intercepts=intercepts,
                    neuron_type=neuron_type))

                try:
                    # if the NengoLib is installed, use it
                    # to optimize encoder placement
                    self.adapt_ens.encoders = (
                        nengolib.stats.ScatteredHypersphere(
                            surface=True))
                except AttributeError:
                    pass

                # proved context signal to adaptive population
                nengo.Connection(
                    qdq_input,
                    self.adapt_ens[ii][:N_DIMS],
                    synapse=0.005)

                # load weights from file if they exist, otherwise use zeros
                weights_file = os.path.expanduser(weights_file)
                print('Backend: ', backend)
                print('Weights file: %s' % weights_file)

                if backend == 'nengo_spinnaker':
                    if os.path.isfile('%s' % weights_file):
                        transform = np.load(weights_file)['weights']
                        transform = np.squeeze(transform).T
                        print('Loading transform: \n', transform)
                        print('Transform all zeros: ', np.allclose(transform, 0))
                    else:
                        print('Transform is zeros')
                        transform = np.zeros((self.adapt_ens[ii].n_neurons,
                                              self.robot_config.N_JOINTS)).T
                else:
                    if os.path.isfile('%s' % weights_file[ii]):
                        transform = np.load(weights_file[ii])['weights'][-1][0]
                        print('Loading transform: \n', transform)
                        print('Transform all zeros: ', np.allclose(transform, 0))
                    else:
                        print('Transfom is zeros')
                        transform = np.zeros((self.adapt_ens[ii].n_neurons,
                                              self.robot_config.N_JOINTS)).T

                # set up learning connections
                if backend == 'nengo_spinnaker':
                    conn_learn.append(
                        nengo.Connection(
                            self.adapt_ens[ii],
                            output,
                            learning_rule_type=nengo.PES(pes_learning_rate),
                            solver=DummySolver(transform.T)))
                else:
                    conn_learn.append(
                        nengo.Connection(
                            self.adapt_ens[ii].neurons,
                            output,
                            learning_rule_type=nengo.PES(pes_learning_rate),
                            transform=transform))

                # Allow filtering of error signal
                def gate_error(x):
                    """ Filters the error, if filter_error is True """

                    if filter_error:
                        if np.linalg.norm(x) > 2.0:
                            x /= np.linalg.norm(x) * 0.5
                    return x
                nengo.Connection(u_input, conn_learn[ii].learning_rule,
                                 # invert to provide error not reward
                                 transform=-1,
                                 function=gate_error,
                                 synapse=0.01)

        nengo.cache.DecoderCache().invalidate()
        if backend == 'nengo':
            self.sim = nengo.Simulator(nengo_model, dt=.001)
        elif backend == 'nengo_ocl':
            try:
                import nengo_ocl
            except ImportError:
                raise Exception('Nengo OCL not installed, ' +
                                'cannot use this backend.')
            import pyopencl as cl
            # Here, the context would be to use all devices from platform [0]
            ctx = cl.Context(cl.get_platforms()[0].get_devices())
            self.sim = nengo_ocl.Simulator(nengo_model, context=ctx, dt=.001)
        elif backend == 'nengo_spinnaker':
            try:
                import nengo_spinnaker
            except ImportError:
                raise Exception('Nengo SpiNNaker not installed, ' +
                                'cannot use this backend.')
            self.sim = nengo_spinnaker.Simulator(nengo_model)
            self.q = np.zeros(self.robot_config.N_JOINTS)
            self.dq = np.zeros(self.robot_config.N_JOINTS)
            self.training_signal = np.zeros(self.robot_config.N_JOINTS)
            # start running the spinnaker model
            self.sim.async_run_forever()
        else:
            raise Exception('Invalid backend specified')
        self.backend = backend

    def generate(self, q, dq, training_signal):
        """ Generates the control signal

        Parameters
        ----------
        q : numpy.array
            the current joint angles [radians]
        dq : numpy.array
            the current joint velocities [radians/second]
        training_signal : numpy.array
            the learning signal to drive adaptation
        """

        # store local copies to feed in to the adaptive population
        self.q = q
        self.dq = dq
        self.training_signal = training_signal

        # run the simulation t generate the adaptive signal
        if self.backend == 'nengo' or self.backend == 'nengo_ocl':
            self.sim.run(time_in_seconds=.001, progress_bar=False)
        elif self.backend == 'nengo_spinnaker':
            # update spinnaker inputs
            self.sim.async_update()
        else:
            self.sim.run(time_in_seconds=.001)

        return self.u_adapt

    def weights_location(self, trial=None, run=None, test_name='test'):
        """ Search for most recent saved weights

        Searches the specified test_name for the highest numbered run in the
        highest numbered trial, unless otherwise specified. Returns the file
        location and the number of the most recent run.

        Parameters
        ----------
        trial: int, optional (Default: None)
            if doing multiple trials of n runs to average over.
            if set to None it will search for the most recent trial
            to save to, saving to 'trial0' if none exist
        run: int, optional (Default: None)
            the current run number. This value will automatically
            increment based on the last run saved in the test_name
            folder. The user can specify a run if they desire to
            overwrite a previous run. If set to None then the test_name
            folder will be searched for the next run to save as
        test_name: string, optional (Default: 'test')
            the test name to save the weights under
        """

        test_name = cache_dir + '/saved_weights/' + test_name

        # if trial is not None, then the user has specified what trial to save the
        # current weights in
        if trial is not None:
            test_name += '/trial%i' % trial
        # If no trial is specified, check if there is already one created, if not
        # then save the run to 'trial0'
        else:
            prev_trials = glob.glob(test_name + '/trial???')
            if prev_trials:
                test_name = max(prev_trials)
            else:
                test_name += '/trial0'

        # check if the provided test_name exists, if not, create it
        if not os.path.exists(test_name):
            print("The provided directory does not exist, creating folder...")
            os.makedirs(test_name)

        # if no run is specified, check what the most recent run saved is and save as
        # the next one in the series. If no run exists then save it as 'run0'
        # if saving, if loading throw an error that no weights file exists
        if run is None:
            prev_runs = glob.glob(test_name + '/*.npz')
            if prev_runs:
                run = max(prev_runs)
                run_num = int(run[run.rfind('/')+4:run.find('.npz')])
            else:
                run_num = -1
        else:
            # save as the run specified by the user
            run_num = run

        return [test_name, run_num]

    def save_weights(self, **kwargs):
        """ Save the current weights to the specified test_name folder

        Save weights for individual runs. A group of runs is
        classified as a trial. Multiple trials can then be used
        to average over a set of learned runs. If trial or run are set to None
        then the test_name location will be searched for the highest numbered
        folder and file respectively
        """

        [test_name, run_num] = self.weights_location(**kwargs)

        print('saving weights as run%i'% (run_num+1))
        if self.backend == 'nengo_spinnaker':
            import nengo_spinnaker.utils.learning
            self.sim.close()
            time.sleep(5)
            np.savez_compressed(
                test_name + '/run%i' % (run_num + 1),
                weights=([nengo_spinnaker.utils.learning.get_learnt_decoders(
                         self.sim, self.adapt_ens[0])]))
            print('Spinnaker output: ', nengo_spinnaker.utils.learning.get_learnt_decoders(self.sim,
                self.adapt_ens[0]))

        else:
            np.savez_compressed(
                test_name + '/run%i' % (run_num + 1),
                weights=[self.sim.data[self.probe_weights[0]]])

    def load_weights(self, **kwargs):
        """ Loads the most recently saved weights unless otherwise specified

        Checks test_name to load the most recent set of weights, unless
        otherwise specified in trial and run. Assumes the most recent is the
        highest trial and run number.
        """

        [test_name, run_num] = self.weights_location(**kwargs)

        if run_num == -1:
            print('No weights found in the specified directory...')
            weights_file = ['']*self.n_adapt_pop
        else:
            weights_file = test_name + '/run%i.npz' % run_num
        return weights_file

class DummySolver(nengo.solvers.Solver):
    """ A Nengo weights solver that returns a provided set of weights.
    """


    def __init__(self, fixed):
        self.fixed = fixed
        self.weights = False

    def __call__(self, A, Y, rng=None, E=None):
        return self.fixed, {}


class AreaIntercepts(nengo.dists.Distribution):
    """ Generate an optimally distributed set of intercepts in
    high-dimensional space.
    """
    dimensions = nengo.params.NumberParam('dimensions')
    base = nengo.dists.DistributionParam('base')

    def __init__(self, dimensions, base=nengo.dists.Uniform(-1, 1)):
        super(AreaIntercepts, self).__init__()
        self.dimensions = dimensions
        self.base = base

    def __repr(self):
        return ("AreaIntercepts(dimensions=%r, base=%r)" %
                (self.dimensions, self.base))

    def transform(self, x):
        sign = 1
        if x > 0:
            x = -x
            sign = -1
        return sign * np.sqrt(1 - scipy.special.betaincinv(
            (self.dimensions + 1) / 2.0, 0.5, x + 1))

    def sample(self, n, d=None, rng=np.random):
        s = self.base.sample(n=n, d=d, rng=rng)
        for i in range(len(s)):
            s[i] = self.transform(s[i])
        return s
