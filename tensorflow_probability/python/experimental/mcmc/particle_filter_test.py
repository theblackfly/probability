# Copyright 2020 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the _License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Tests for particle filtering."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import tensorflow.compat.v2 as tf
import tensorflow_probability as tfp
from tensorflow_probability.python.internal import prefer_static
from tensorflow_probability.python.internal import test_util

tfb = tfp.bijectors
tfd = tfp.distributions


@test_util.test_all_tf_execution_regimes
class _ParticleFilterTest(test_util.TestCase):

  def test_random_walk(self):
    initial_state_prior = tfd.JointDistributionNamed({
        'position': tfd.Deterministic(0.)})

    # Biased random walk.
    def particle_dynamics(_, previous_state):
      state_shape = tf.shape(previous_state['position'])
      return tfd.JointDistributionNamed({
          'position': tfd.TransformedDistribution(
              tfd.Bernoulli(probs=tf.broadcast_to(0.75, state_shape),
                            dtype=self.dtype),
              tfb.Shift(previous_state['position']))})

    # Completely uninformative observations allowing a test
    # of the pure dynamics.
    def particle_observations(_, state):
      state_shape = tf.shape(state['position'])
      return tfd.Uniform(low=tf.broadcast_to(-100.0, state_shape),
                         high=tf.broadcast_to(100.0, state_shape))

    observations = tf.zeros((9,), dtype=self.dtype)
    trajectories, _ = self.evaluate(
        tfp.experimental.mcmc.infer_trajectories(
            observations=observations,
            initial_state_prior=initial_state_prior,
            transition_fn=particle_dynamics,
            observation_fn=particle_observations,
            num_particles=16384,
            seed=test_util.test_seed()))
    position = trajectories['position']

    # The trajectories have the following properties:
    # 1. they lie completely in the range [0, 8]
    self.assertAllInRange(position, 0., 8.)
    # 2. each step lies in the range [0, 1]
    self.assertAllInRange(position[1:] - position[:-1], 0., 1.)
    # 3. the expectation and variance of the final positions are 6 and 1.5.
    self.assertAllClose(tf.reduce_mean(position[-1]), 6., atol=0.1)
    self.assertAllClose(tf.math.reduce_variance(position[-1]), 1.5, atol=0.1)

  def test_batch_of_filters(self):

    batch_shape = [3, 2]
    num_particles = 1000
    num_timesteps = 40

    # Batch of priors on object 1D positions and velocities.
    initial_state_prior = tfd.JointDistributionNamed({
        'position': tfd.Normal(loc=0., scale=tf.ones(batch_shape)),
        'velocity': tfd.Normal(loc=0., scale=tf.ones(batch_shape) * 0.1)})

    def transition_fn(_, previous_state):
      return tfd.JointDistributionNamed({
          'position': tfd.Normal(
              loc=previous_state['position'] +previous_state['velocity'],
              scale=0.1),
          'velocity': tfd.Normal(loc=previous_state['velocity'], scale=0.01)})

    def observation_fn(_, state):
      return tfd.Normal(loc=state['position'], scale=0.1)

    # Batch of synthetic observations, .
    true_initial_positions = np.random.randn(*batch_shape).astype(self.dtype)
    true_velocities = 0.1 * np.random.randn(
        *batch_shape).astype(self.dtype)
    observed_positions = (
        true_velocities *
        np.arange(num_timesteps).astype(self.dtype)[..., None, None] +
        true_initial_positions)

    particles, parent_indices, step_log_marginal_likelihoods = self.evaluate(
        tfp.experimental.mcmc.particle_filter(
            observations=observed_positions,
            initial_state_prior=initial_state_prior,
            transition_fn=transition_fn,
            observation_fn=observation_fn,
            num_particles=num_particles,
            seed=test_util.test_seed()))

    self.assertAllEqual(particles['position'].shape,
                        [num_timesteps] + batch_shape + [num_particles])
    self.assertAllEqual(particles['velocity'].shape,
                        [num_timesteps] + batch_shape + [num_particles])
    self.assertAllEqual(parent_indices.shape,
                        [num_timesteps] + batch_shape + [num_particles])
    self.assertAllEqual(step_log_marginal_likelihoods.shape,
                        [num_timesteps] + batch_shape)

    self.assertAllClose(
        self.evaluate(
            tf.reduce_mean(particles['position'], axis=-1)),
        observed_positions,
        atol=0.1)

    self.assertAllClose(
        self.evaluate(
            tf.reduce_mean(particles['velocity'], axis=(0, -1))),
        true_velocities,
        atol=0.05)

    # Uncertainty in velocity should decrease over time.
    velocity_stddev = self.evaluate(
        tf.math.reduce_std(particles['velocity'], axis=-1))
    self.assertAllLess((velocity_stddev[-1] - velocity_stddev[0]), 0.)

    trajectories = self.evaluate(
        tfp.experimental.mcmc.reconstruct_trajectories(particles,
                                                       parent_indices))
    self.assertAllEqual([num_timesteps] + batch_shape + [num_particles],
                        trajectories['position'].shape)
    self.assertAllEqual([num_timesteps] + batch_shape + [num_particles],
                        trajectories['velocity'].shape)

  def test_reconstruct_trajectories_toy_example(self):
    particles = tf.convert_to_tensor([[1, 2, 3], [4, 5, 6,], [7, 8, 9]])
    # 1  --  4  -- 7
    # 2  \/  5  .- 8
    # 3  /\  6 /-- 9
    parent_indices = tf.convert_to_tensor([[0, 1, 2], [0, 2, 1], [0, 2, 2]])

    trajectories = self.evaluate(
        tfp.experimental.mcmc.reconstruct_trajectories(particles,
                                                       parent_indices))
    self.assertAllEqual(
        np.array([[1, 2, 2], [4, 6, 6], [7, 8, 9]]), trajectories)

  def test_epidemiological_model(self):
    # A toy, discrete version of an SIR (Susceptible, Infected, Recovered)
    # model (https://en.wikipedia.org/wiki/Compartmental_models_in_epidemiology)

    population_size = 1000
    infection_rate = tf.convert_to_tensor(1.1)
    infectious_period = tf.convert_to_tensor(8.0)

    initial_state_prior = tfd.JointDistributionNamed({
        'susceptible': tfd.Deterministic(999.),
        'infected': tfd.Deterministic(1.),
        'new_infections': tfd.Deterministic(1.),
        'new_recoveries': tfd.Deterministic(0.)})

    # Dynamics model: new infections and recoveries are given by the SIR
    # model with Poisson noise.
    def infection_dynamics(_, previous_state):
      new_infections = tfd.Poisson(
          infection_rate * previous_state['infected'] *
          previous_state['susceptible'] / population_size)
      new_recoveries = tfd.Poisson(previous_state['infected'] /
                                   infectious_period)

      def susceptible(new_infections):
        return tfd.Deterministic(
            prefer_static.maximum(
                0., previous_state['susceptible'] - new_infections))

      def infected(new_infections, new_recoveries):
        return tfd.Deterministic(
            prefer_static.maximum(
                0.,
                previous_state['infected'] + new_infections - new_recoveries))

      return tfd.JointDistributionNamed({
          'new_infections': new_infections,
          'new_recoveries': new_recoveries,
          'susceptible': susceptible,
          'infected': infected})

    # Observation model: each day we detect new cases, noisily.
    def infection_observations(_, state):
      return tfd.Poisson(state['infected'])

    # pylint: disable=bad-whitespace
    observations = tf.convert_to_tensor([
        0.,     4.,   1.,   5.,  23.,  27.,  75., 127., 248., 384., 540., 683.,
        714., 611., 561., 493., 385., 348., 300., 277., 249., 219., 216., 174.,
        132., 122., 115.,  99.,  76.,  84.,  77.,  56.,  42.,  56.,  46.,  38.,
        34.,   44.,  25.,  27.])
    # pylint: enable=bad-whitespace

    trajectories, _ = self.evaluate(
        tfp.experimental.mcmc.infer_trajectories(
            observations=observations,
            initial_state_prior=initial_state_prior,
            transition_fn=infection_dynamics,
            observation_fn=infection_observations,
            num_particles=100,
            seed=test_util.test_seed()))

    # The susceptible population should decrease over time.
    self.assertAllLessEqual(
        trajectories['susceptible'][1:, ...] -
        trajectories['susceptible'][:-1, ...],
        0.0)

  def test_data_driven_proposal(self):

    num_particles = 100
    observations = tf.convert_to_tensor([60., -179.2, 1337.42])

    # Define a system constrained primarily by observations, where proposing
    # from the dynamics would be a bad fit.
    initial_state_prior = tfd.Normal(loc=0., scale=1e6)
    transition_fn = (
        lambda _, previous_state: tfd.Normal(loc=previous_state, scale=1e6))
    observation_fn = lambda _, state: tfd.Normal(loc=state, scale=0.1)
    initial_state_proposal = tfd.Normal(loc=observations[0], scale=0.1)
    proposal_fn = (lambda step, state: tfd.Normal(  # pylint: disable=g-long-lambda
        loc=tf.ones_like(state) * observations[step + 1], scale=1.0))

    particles, parent_indices, _ = self.evaluate(
        tfp.experimental.mcmc.particle_filter(
            observations=observations,
            initial_state_prior=initial_state_prior,
            transition_fn=transition_fn,
            observation_fn=observation_fn,
            num_particles=num_particles,
            initial_state_proposal=initial_state_proposal,
            proposal_fn=proposal_fn,
            seed=test_util.test_seed()))
    trajectories = self.evaluate(
        tfp.experimental.mcmc.reconstruct_trajectories(particles,
                                                       parent_indices))
    self.assertAllClose(trajectories,
                        tf.convert_to_tensor(
                            tf.convert_to_tensor(observations)[..., None] *
                            tf.ones([num_particles])), atol=1.0)

  def test_estimated_prob_approximates_true_prob(self):

    # Draw simulated data from a 2D linear Gaussian system.
    initial_state_prior = tfd.MultivariateNormalDiag(
        loc=0., scale_diag=(1., 1.))
    transition_matrix = tf.convert_to_tensor([[1., -0.5], [0.4, -1.]])
    transition_noise = tfd.MultivariateNormalTriL(
        loc=1., scale_tril=tf.convert_to_tensor([[0.3, 0], [-0.1, 0.2]]))
    observation_matrix = tf.convert_to_tensor([[0.1, 1.], [1., 0.2]])
    observation_noise = tfd.MultivariateNormalTriL(
        loc=-0.3, scale_tril=tf.convert_to_tensor([[0.5, 0], [0.1, 0.5]]))
    model = tfd.LinearGaussianStateSpaceModel(
        num_timesteps=20,
        initial_state_prior=initial_state_prior,
        transition_matrix=transition_matrix,
        transition_noise=transition_noise,
        observation_matrix=observation_matrix,
        observation_noise=observation_noise)
    observations = self.evaluate(
        model.sample(seed=test_util.test_seed()))
    (lps, filtered_means,
     _, _, _, _, _) = self.evaluate(model.forward_filter(observations))

    # Approximate the filtering means and marginal likelihood(s) using
    # the particle filter.
    # pylint: disable=g-long-lambda
    (particles, _, estimated_step_log_marginal_likelihoods) = self.evaluate(
        tfp.experimental.mcmc.particle_filter(
            observations=observations,
            initial_state_prior=initial_state_prior,
            transition_fn=lambda _, previous_state: tfd.MultivariateNormalTriL(
                loc=transition_noise.loc + tf.linalg.matvec(
                    transition_matrix, previous_state),
                scale_tril=transition_noise.scale_tril),
            observation_fn=lambda _, state: tfd.MultivariateNormalTriL(
                loc=observation_noise.loc + tf.linalg.matvec(
                    observation_matrix, state),
                scale_tril=observation_noise.scale_tril),
            num_particles=1000,
            seed=test_util.test_seed()))
    # pylint: enable=g-long-lambda

    particle_means = np.mean(particles, axis=1)
    self.assertAllClose(filtered_means, particle_means, atol=0.1, rtol=0.1)

    self.assertAllClose(
        lps, estimated_step_log_marginal_likelihoods, atol=0.5)

  def test_model_can_use_state_history(self):

    initial_state_prior = tfd.JointDistributionNamed(
        {'x': tfd.Poisson(1.)})

    # Deterministic dynamics compute a Fibonacci sequence.
    def fibbonaci_transition_fn(step, state, state_history):
      del step
      del state
      return tfd.JointDistributionNamed(
          {'x': tfd.Deterministic(
              tf.reduce_sum(state_history['x'][-2:], axis=0))})

    # We'll observe the ratio of the current and previous state.
    def observe_ratio_of_last_two_states_fn(_, state, state_history=None):
      ratio = tf.ones_like(state['x'])
      if state_history is not None:
        ratio = state['x'] / (state_history['x'][-1] + 1e-6)  # avoid div. by 0.
      return tfd.Normal(loc=ratio, scale=0.1)

    # The ratios between successive terms of a Fibbonaci sequence
    # should, in the limit, approach the golden ratio.
    golden_ratio = (1. + np.sqrt(5.)) / 2.
    observed_ratios = np.array([golden_ratio] * 10).astype(self.dtype)

    trajectories, lps = self.evaluate(
        tfp.experimental.mcmc.infer_trajectories(
            observed_ratios,
            initial_state_prior=initial_state_prior,
            transition_fn=fibbonaci_transition_fn,
            observation_fn=observe_ratio_of_last_two_states_fn,
            num_particles=100,
            num_steps_state_history_to_pass=2,
            seed=test_util.test_seed()))

    # Verify that we actually produced Fibonnaci sequences.
    self.assertAllClose(
        trajectories['x'][2:],
        trajectories['x'][1:-1] + trajectories['x'][:-2])

    # Ratios should get closer to golden as the series progresses, so
    # likelihoods will increase.
    self.assertAllGreater(lps[2:] - lps[:-2], 0.0)

    # Any particles that sampled initial values of 0. should have been
    # discarded, since those lead to degenerate series that do not approach
    # the golden ratio.
    self.assertAllGreaterEqual(trajectories['x'][0], 1.)

  def test_model_can_use_observation_history(self):

    weights = np.array([0.1, -0.2, 0.7]).astype(self.dtype)

    # Define an autoregressive model on observations. This ignores the
    # state entirely; it depends only on previous observations.
    initial_state_prior = tfd.JointDistributionNamed(
        {'dummy_state': tfd.Deterministic(0.)})
    def dummy_transition_fn(_, state, **kwargs):
      del kwargs
      return tfd.JointDistributionNamed(
          tf.nest.map_structure(tfd.Deterministic, state))
    def autoregressive_observation_fn(step, _, observation_history=None):
      loc = 0.
      if observation_history is not None:
        num_terms = prefer_static.minimum(step, len(weights))
        usable_weights = tf.convert_to_tensor(weights)[-num_terms:]
        loc = tf.reduce_sum(usable_weights * observation_history)
      return tfd.Normal(loc, 1.0)

    # Manually compute the conditional log-probs of a series of observations
    # under the autoregressive model.
    observations = np.array(
        [0.1, 3., -0.7, 1.1, 0., 14., -3., 5.8]).astype(self.dtype)
    expected_locs = []
    for current_step in range(len(observations)):
      start_step = max(0, current_step - len(weights))
      context_length = current_step - start_step
      expected_locs.append(
          np.sum(observations[start_step : current_step] *
                 weights[len(weights)-context_length:]))
    expected_lps = self.evaluate(
        tfd.Normal(expected_locs, scale=1.0).log_prob(observations))

    # Check that the particle filter gives the same log-probs.
    _, _, lps = self.evaluate(tfp.experimental.mcmc.particle_filter(
        observations,
        initial_state_prior=initial_state_prior,
        transition_fn=dummy_transition_fn,
        observation_fn=autoregressive_observation_fn,
        num_particles=2,
        num_steps_observation_history_to_pass=len(weights)))
    self.assertAllClose(expected_lps, lps)


class ParticleFilterTestFloat32(_ParticleFilterTest):
  dtype = np.float32


del _ParticleFilterTest


if __name__ == '__main__':
  tf.test.main()
