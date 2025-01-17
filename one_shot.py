import argparse
import logging
import os
import sys
import time
from threading import Thread
from utils import ResNet, SetRepresentation, put_new_data, load_data, \
    predictive_lb, predictive_ll, lower_bound, likelihood_classification
from classification import one_shot_classification, cos_sim, blackbox_classification
import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf
from matplotlib import gridspec
import ast

import scg

parser = argparse.ArgumentParser()
parser.add_argument('--episode', type=int, default=10)
parser.add_argument('--checkpoint', type=str, default='GMN/gmn.ckpt')
parser.add_argument('--hidden_dim', type=int, default=50)
parser.add_argument('--test', type=int, default=None)
parser.add_argument('--classification', type=int, default=None)
parser.add_argument('--max_classes', type=int, default=2)
parser.add_argument('--test_episodes', type=int, default=10)
# parser.add_argument('--reconstructions', action='store_const', const=True)
parser.add_argument('--reconstructions', type= ast.literal_eval)

parser.add_argument('--generate', type=int, default=None)
parser.add_argument('--test_dataset', type=str, default='data/test_small.npz')
parser.add_argument('--train_dataset', type=str, default='data/train_small.npz')
parser.add_argument('--batch', type=int, default=256)
parser.add_argument('--seed', type=int, default=123)
parser.add_argument('--l2', type=float, default=0.)
parser.add_argument('--prior_hops', type=int, default=1)
parser.add_argument('--hops', type=int, default=1)
parser.add_argument('--shots', type=int, default=1)
parser.add_argument('--likelihood_classification', type=int, default=None)
# parser.add_argument('--no_dummy', action='store_const', const=True)
parser.add_argument('--no_dummy', type= ast.literal_eval)
parser.add_argument('--classes', type=str, default=None)
# parser.add_argument('--conditional', action='store_const', const=True)
# parser.add_argument('--prior_entropy', action='store_const', const=True)

parser.add_argument('--conditional',type= ast.literal_eval)
parser.add_argument('--prior_entropy', type= ast.literal_eval)



args = parser.parse_args()

tf.set_random_seed(args.seed)
np.random.seed(args.seed)

print(args.reconstructions,args.no_dummy,args.conditional,args.prior_entropy)

if args.classification is not None:
    args.batch = args.max_classes
    args.episode = args.max_classes * args.shots + 1
elif args.likelihood_classification is not None:
    args.batch = args.likelihood_classification
    args.episode = args.shots + 1
elif args.generate is not None:
    args.batch = args.generate
elif args.prior_entropy:
    args.batch = 1

if args.classes is not None:
    args.classes = np.array(map(int, args.classes.split(' ')))

start_from = 0
if args.conditional is not None:
    start_from = args.max_classes
    args.no_dummy = True

data_dim = 28*28
episode_length = args.episode


'''
generation:
1. latent variables
2. generated images
'''
class GenerativeModel:
    def __init__(self, hidden_dim, state_dim):
        self.hidden_dim = hidden_dim

        self.hp = scg.Affine(state_dim, 200, 'prelu', scg.norm_init(scg.he_normal))
        self.mu = scg.Affine(200, self.hidden_dim, None, scg.he_normal)
        self.pre_sigma = scg.Affine(200, self.hidden_dim, None, scg.he_normal)
        self.prior = scg.Normal(self.hidden_dim)

        self.h0 = scg.Affine(hidden_dim + state_dim, 3*3*32, fun=None, init=scg.he_normal)
        self.h1 = ResNet.section([3, 3, 32], [2, 2], 32, 2, [2, 2], downscale=False)
        self.h2 = ResNet.section([6, 6, 32], [3, 3], 16, 2, [3, 3], downscale=False)
        self.h3 = ResNet.section([13, 13, 16], [4, 4], 16, 2, [3, 3], downscale=False)
        self.conv = scg.Convolution2d([28, 28, 16], [1, 1], 1, padding='VALID',
                                      init=scg.he_normal)

    ## mu and sigma is from state, which is from feature
    ## hidden_name: latent variable [z_0.z_9] for each image
    def generate_prior(self, state, hidden_name):
        hp = self.hp(input=state)
        z = self.prior(name=hidden_name, mu=self.mu(input=hp, name=hidden_name + '_prior_mu'),
                       pre_sigma=self.pre_sigma(input=hp, name=hidden_name + '_prior_sigma'))
        return z

    ## z is sampleed from normal distribution
    ## param is from response and state
    ## observed_name: generated image for observed images[x_0.x_9]
    def generate(self, z, param, observed_name):
        h = self.h0(input=scg.concat([z, param]))
        h = self.h1(h)
        h = self.h2(h)
        h = self.h3(h)

        h = self.conv(input=h, name=observed_name + '_logit')
        return scg.Bernoulli()(logit=h, name=observed_name)

'''
recognition:
1. generated feature
2. generated z a normal distribution
'''
class RecognitionModel:
    def __init__(self, hidden_dim, state_dim):
        self.hidden_dim = hidden_dim

        self.init = scg.norm_init(scg.he_normal)

        self.h1 = ResNet.section([28, 28, 1], [4, 4], 16, 2, [3, 3])
        self.h2 = ResNet.section([13, 13, 16], [3, 3], 32, 2, [3, 3])
        self.h3 = ResNet.section([6, 6, 32], [2, 2], 32, 2, [2, 2])

        self.features_dim = 3 * 3 * 32  # np.prod(self.h3.shape)

        self.mu = scg.Affine(self.features_dim + state_dim, hidden_dim)
        self.sigma = scg.Affine(self.features_dim + state_dim, hidden_dim)

    def get_features(self, obs):
        h = self.h1(obs)
        h = self.h2(h)
        h = self.h3(h)

        return h

    def recognize(self, h, param, hidden_name):
        h = scg.concat([h, param])
        mu = self.mu(input=h, name=hidden_name + '_mu')
        sigma = self.sigma(input=h, name=hidden_name + '_sigma')
        z = scg.Normal(self.hidden_dim)(mu=mu, pre_sigma=sigma, name=hidden_name)
        return z


class VAE(object):
    @staticmethod
    def hidden_name(step):
        return 'z_' + str(step)

    @staticmethod
    def observed_name(step):
        return 'x_' + str(step)

    @staticmethod
    def params_name(step):
        return 'theta_' + str(step)

    def __init__(self, input_data, hidden_dim, gen, rec):
        state_dim = 200
        self.num_steps = args.hops
        self.prior_steps = args.prior_hops
        self.matching_dim = 200

        with tf.variable_scope('recognition') as vs:
            self.rec = rec(hidden_dim, state_dim + 288)
            self.features_dim = self.rec.features_dim
            self._rec_query = scg.Affine(state_dim + self.features_dim, self.matching_dim,
                                         fun=None, init=scg.norm_init(scg.he_normal))
            self._rec_strength = scg.Affine(state_dim, 1, init=scg.norm_init(scg.he_normal))

        with tf.variable_scope('generation') as vs:
            self.gen = gen(hidden_dim, state_dim + self.features_dim)
            self._gen_query = scg.Affine(state_dim + hidden_dim, self.matching_dim,
                                         fun=None, init=scg.norm_init(scg.he_normal))
            self._gen_strength = scg.Affine(state_dim, 1, init=scg.norm_init(scg.he_normal))

            self._prior_query = scg.Affine(state_dim, self.matching_dim, fun=None, init=scg.norm_init(scg.he_normal))
            self._prior_strength = scg.Affine(state_dim, 1, init=scg.norm_init(scg.he_normal))
            self.prior_repr = SetRepresentation(self.features_dim, self.matching_dim, state_dim)

        with tf.variable_scope('both') as vs:
            self.set_repr = SetRepresentation(self.features_dim, self.matching_dim, state_dim)

        self.z = [None] * episode_length
        self.x = [None] * (episode_length+1)

        # allocating observations
        '''
        1.self.obs: input_data
        2.self.feature
        '''
        self.obs = [None] * episode_length
        for t in xrange(episode_length):
            current_data = input_data[:, t, :]
            self.obs[t] = scg.Constant(value=current_data, shape=[28*28])(name=VAE.observed_name(t))

        # pre-computing features
        self.features = []
        for t in xrange(episode_length):
            self.features.append(self.rec.get_features(self.obs[t]))

        for timestep in xrange(episode_length+1):
            dummy = True
            if args.no_dummy and timestep > 0:
                dummy = False

            if timestep < episode_length:
                def rec_query(state):
                    return self._rec_query(input=scg.concat([state, self.features[timestep]]))

                def rec_strength(state):
                    return self._rec_strength(input=state)

                ##core code for GRU
                ## input: features
                ## output: response (256, 288) and state (256, 200) for recognition model
                rec_response, rec_state = self.set_repr.recognize(self.features, timestep, rec_query,
                                                                  self.num_steps, strength=rec_strength,
                                                                  dummy=dummy)
                

                ##recognition model to check normal distribtion z
                ##input:features, response, state
                self.z[timestep] = self.rec.recognize(self.features[timestep], scg.concat([rec_response, rec_state]),
                                                      VAE.hidden_name(timestep))

            self.x[timestep] = self.generate(timestep, dummy=dummy)

    def generate(self, timestep, dummy=True):
        def prior_query(state):
            return self._prior_query(input=state)

        def prior_strength(state):
            return self._prior_strength(input=state)

        ## core code for GRU
        ## generating responce(256, 288) and state(256, 200)
        prior_response, prior_state = self.prior_repr.recognize(self.features, timestep, prior_query, self.prior_steps,
                                                                strength=prior_strength, dummy=dummy)

        z_prior = self.gen.generate_prior(scg.concat([prior_response, prior_state]), VAE.hidden_name(timestep))

        def gen_query(state):
            return self._gen_query(input=scg.concat([state, z_prior]))

        def gen_strength(state):
            return self._gen_strength(input=state)

        ## core code for GRU
        ## generate respnce (256, 288) and state(256, 200) for generated image
        gen_response, gen_state = self.set_repr.recognize(self.features, timestep, gen_query,
                                                          self.num_steps, strength=gen_strength,
                                                          dummy=dummy)
        return self.gen.generate(z_prior, scg.concat([gen_response, gen_state]), VAE.observed_name(timestep))

    def sample(self, cache=None):
        # genrating z and x
        # z[i]: (20, 50)
        # x[i]: (20, 784)
        if cache is None:
            cache = {}
        for i in xrange(episode_length):
            time_start = time.time()
            self.z[i].backtrace(cache)
            self.x[i].backtrace(cache)
            print(self.z[i].backtrace(),self.x[i].backtrace())
            print i, time.time() - time_start
        return cache

    ## ll: likelihood
    ## generation: observer name and hidden name
    ## recognition: hidden name
    ## if integrating gan, may insert into this to optimize the model
    def importance_weights(self, cache):
        #generating: z and x
        #recognition: z
        gen_ll = {}
        rec_ll = {}

        # w[t][i] -- likelihood ratio for the i-th object after t objects has been seen
        w = [0.] * episode_length

        ## calculating likelihood of generated z[i] and x[i]
        for i in xrange(episode_length):
            # each samples 
            # z[i] corresponding to likelihood
            # x[i] corresponding to likelihood
            scg.likelihood(self.z[i], cache, rec_ll)
            scg.likelihood(self.x[i], cache, gen_ll)

            # VAE.observed_name(i):x_0, ... , x_9, generated x
            # VAE.hidden_name(i):z_0, ... , z_9
            ## generator: the likelihood larger, the better
            ## recognition: the likelihood smaller, the better
            w[i] = gen_ll[VAE.observed_name(i)] + gen_ll[VAE.hidden_name(i)] - rec_ll[VAE.hidden_name(i)]

        w = tf.stack(w)

        return w, [gen_ll, rec_ll]


data_queue = tf.FIFOQueue(1000, tf.float32, shapes=[episode_length, data_dim])

new_data = tf.placeholder(tf.float32, [None, episode_length, data_dim])
enqueue_op = data_queue.enqueue_many(new_data)
batch_size = args.batch if args.test is None else args.test
input_data = data_queue.dequeue_many(batch_size)

with tf.variable_scope('model'):
    vae = VAE(input_data, args.hidden_dim, GenerativeModel, RecognitionModel)
train_samples = vae.sample(None)

# loss and likelihood from generator and recognition model
weights, ll = vae.importance_weights(train_samples)


# loss calculation
# measuring the hidden latent variables
# another calculation method
def effective_sample_size(gen_ll, rec_ll):
    w = []
    for t in xrange(episode_length):
        w_t = gen_ll[VAE.hidden_name(t)] - rec_ll[VAE.hidden_name(t)]
        w.append(w_t)
    w = tf.stack(w)
    max_w = tf.reduce_max(w, 0)
    adjusted_w = w - max_w
    exp_w = tf.exp(adjusted_w)
    ess = tf.square(tf.reduce_sum(exp_w, 0)) / tf.reduce_sum(tf.square(exp_w), 0)
    return ess


# entropy calculation method
# another calculation method
def entropy(samples):
    result = []
    for t in xrange(episode_length):
        sigma = tf.nn.softplus(tf.clip_by_value(samples[VAE.hidden_name(t) + '_prior_sigma'], -10., 10.))
        h = 0.5 * (1. + np.log(np.pi) + np.log(2.) + 2 * tf.log(sigma))
        result.append(tf.reduce_sum(h))
        # result.append(tf.reduce_mean(sigma))
    return tf.stack(result)


## calculate other evaluation metric from the total loss
## train_pred_lb: mean value of loss for each training samples
## train_pred_ll: log mean, another calculation method
## prior_entropy: generated the latent z and z is from recogniton model, measuring the difference of sigma
train_pred_lb = predictive_lb(weights)
train_pred_ll = predictive_ll(weights)
prior_entropy = entropy(train_samples)


## lower_bound: why start from???
vlb_gen = lower_bound(weights, start_from)

global_step = tf.Variable(0, trainable=False)
learning_rate = tf.placeholder(tf.float32)

epoch_passed = tf.Variable(0)
increment_passed = epoch_passed.assign_add(1)


## regulization for all trainable parameters
reg = 0.
for var in tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='model'):
    reg += tf.reduce_sum(tf.square(var))



## optimization: lower_bound and regularization
## objective: lower_bound + regularizor
train_objective = -vlb_gen + args.l2 * reg

## make sure the optimization objects
train_op = tf.train.AdamOptimizer(beta2=0.99, epsilon=1e-8, learning_rate=learning_rate,
                                  use_locking=False).minimize(train_objective, global_step)

saver = tf.train.Saver()
with tf.Session() as sess:
    log = logging.getLogger()
    log.setLevel(10)
    log.addHandler(logging.StreamHandler())
    if args.checkpoint is not None:
        log.addHandler(logging.FileHandler(args.checkpoint + '.log'))

    def data_loop(coordinator=None):
        # train_data: (964, 20, 784), 964 is classes, 20 is the number of samples in each category
        train_data = load_data(args.train_dataset) if not args.reconstructions else load_data(args.test_dataset)
        # batch: (1, 10, 784) 
        batch = np.zeros((1, episode_length, data_dim))
        # test_data = np.load('data/test_small.npz')

        while coordinator is None or not coordinator.should_stop():
            put_new_data(train_data, batch, args.max_classes, conditional=args.conditional)
            sess.run(enqueue_op, feed_dict={new_data: batch})

    # Thread management, close or open
    coord = tf.train.Coordinator()



    if args.checkpoint is not None:
        print 'checkpoint found, restoring'
        saver.restore(sess, args.checkpoint)
    else:
        print 'starting from scratch'
        sess.run(tf.initialize_all_variables())


    data_threads = [Thread(target=data_loop, args=[coord]) for i in xrange(1)]

    ## if not testing model: t.start()
    if args.test is None and args.generate is None and args.classification is None and args.likelihood_classification is None:
        for t in data_threads:
            t.start()


    ## the evaluation metric is the average loss: weights is from the genration model and recognition model
    ## each test_episodes has output: refer to the math equation in paper
    def test(full=False):
        test_data = load_data(args.test_dataset)
        avg_predictive_ll = np.zeros(episode_length)
        batch_data = np.zeros((batch_size, episode_length, data_dim), dtype=np.float32)

        target = train_pred_lb if not full else train_pred_ll
        if args.prior_entropy:
            target = prior_entropy
        ## 1000 samples

        ##'full' means test images whether belong to same category,'full' is 
        ## 'full' means that each batch size is same
        for j in xrange(args.test_episodes):
            if full:
                put_new_data(test_data, batch_data[:1, :, :], args.max_classes, conditional=args.conditional)
                for t in xrange(1, batch_data.shape[0]):
                    batch_data[t] = batch_data[0]
            else:
                put_new_data(test_data, batch_data[:, :, :], args.max_classes, conditional=args.conditional)

            ## calculating entropy
            ## direct calculating without training
            ## calculating the average loss
            pred_ll = sess.run(target, feed_dict={input_data: batch_data})
            avg_predictive_ll += (pred_ll - avg_predictive_ll) / (j+1)
            print('each test_episodes')

            msg = '\rtesting %d' % j
            if args.prior_entropy:
                msg = '\rentropy %d' % j
            for t in xrange(episode_length):
                msg += ' %.2f' % avg_predictive_ll[t]
            sys.stdout.write(msg)
            if j == args.test_episodes-1:
                print
                log.info(msg)

    num_epochs = 0
    done_epochs = epoch_passed.eval(sess)


    ## new images is fed into trainde model to calculate the loss value of test images
    if args.test is not None:
        print('1111')
        # same category
        test(full=True)

        # coord.request_stop()
        coord.join(data_threads)
        sys.exit()

    ### reconstruction: train-samples
    ### reconstruction image from the generator for trained images
    ### not feed new test images
    elif args.reconstructions:
        print('2222')
        reconstructions = [None] * episode_length
        for i in xrange(episode_length):
            reconstructions[i] = tf.sigmoid(train_samples[VAE.observed_name(i) + '_logit'][0, :])
        reconstructions = tf.stack(reconstructions)
        original_input = input_data[0, :, :]

        while True:
            sample, original = sess.run([reconstructions, original_input])
            plt.matshow(np.hstack([sample.reshape(28 * episode_length, 28),
                                   original.reshape(28 * episode_length, 28)]),
                        cmap=plt.get_cmap('Greys'))
            plt.show()
            plt.close()

        # coord.request_stop()
        coord.join(data_threads)
        sys.exit()

    ## generating img based on input image
    ## new test images as input
    elif args.generate is not None:
        print('3333')
        train_samples.clear()
        for t in xrange(episode_length+1):
            if t < episode_length:
                train_samples[VAE.observed_name(t)] = input_data[:, t, :]
            obs = vae.generate(t, False if args.no_dummy and t > 0 else True)
            obs.backtrace(train_samples, batch=batch_size)

        data = load_data(args.test_dataset)
        input_batch = np.zeros([batch_size, episode_length, data_dim])

        ##generated images with new testing images as input
        logits = []
        for t in xrange(episode_length+1):
            logits.append(tf.sigmoid(train_samples[VAE.observed_name(t) + '_logit']))
        logits = tf.stack(logits)

        while True:
            classes = put_new_data(data, input_batch[:1], args.max_classes,
                                   classes=args.classes, conditional=args.conditional)
            print 'generating classes ', classes

            for j in xrange(1, input_batch.shape[0]):
                input_batch[j] = input_batch[0]

            # gs = gridspec.GridSpec(episode_length+1, args.generate + 1)
            f, axs = plt.subplots(episode_length+1, args.generate + 1,
                                  sharey=True, sharex=True, squeeze=True,
                                  figsize=(8, 8))

            axs[0, 0].matshow(np.zeros([28, 28]), cmap=plt.get_cmap('gray'))
            for t in xrange(episode_length):
                axs[t+1, 0].matshow(input_batch[0, t, :].reshape(28, 28),
                                  cmap=plt.get_cmap('gray'))

            img = sess.run(logits, feed_dict={input_data: input_batch})

            for t in xrange(episode_length+1):
                for k in xrange(batch_size):
                    if args.conditional and t <= args.max_classes:
                        axs[t, k+1].matshow(np.zeros([28, 28]), cmap=plt.get_cmap('gray'))
                    else:
                        sample = img[t, k].reshape(28, 28)
                        axs[t, k+1].matshow(sample, cmap=plt.get_cmap('Greys'))

            for ax_row in axs:
                for ax in ax_row:
                    ax.set_yticklabels(())
                    ax.set_xticklabels(())
                    ax.title.set_visible(False)
                    plt.subplots_adjust(wspace=0, hspace=0)
                    ax.axis('tight')
                    ax.axis('off')

            plt.savefig('samples.pdf', bbox_inches='tight', pad_inches=0.)
            plt.show()
            plt.close()

        # coord.request_stop()
        coord.join(data_threads)
        sys.exit()

    ##calculating the accuracy of classification
    elif args.classification is not None:
        print('4444')
        mu = []
        for t in xrange(episode_length):
            mu.append(train_samples[VAE.hidden_name(t) + '_mu'])
        mu = tf.squeeze(tf.stack(mu))

        sim = np.zeros([args.max_classes, episode_length - 1])

        def raw_similarities(batch):
            features = np.vstack([batch[:, -1, :], batch[0, :-1, :]])
            sim = cos_sim(features)
            return sim[:, args.max_classes:]

        def compute_similarities(batch):
            batch_mu = sess.run(mu, feed_dict={input_data: batch})
            train_mu = batch_mu[:-1, 0, :]
            test_mu = batch_mu[-1, :, :]
            batch_mu = np.vstack([test_mu, train_mu])

            # for k in xrange(args.max_classes):
            #     for j in xrange(train_mu.shape[0]):
            #         sim[k, j] = np.exp(-np.square(np.linalg.norm(test_mu[k] - train_mu[j])) / 3.)
            # return sim

            sim = cos_sim(batch_mu)
            return sim[:, args.max_classes:]

        test_data = load_data(args.test_dataset)
        accuracy = one_shot_classification(test_data, args.shots, args.max_classes,
                                           compute_similarities, k_neighbours=args.classification,
                                           num_episodes=args.test_episodes)


        log.info('accuracy: %f' % accuracy)

        # coord.request_stop()
        coord.join(data_threads)
        sys.exit()

    ## likelihood
    elif args.likelihood_classification is not None:
        print('5555')
        test_data = load_data(args.test_dataset)
        # prediction = likelihood_classification(weights[-1], args.max_classes,
        #                                        args.likelihood_classification)
        prediction = train_pred_ll[-1]

        def classify(batch):
            return sess.run(prediction, feed_dict={input_data: batch})

        accuracy = blackbox_classification(test_data, args.shots, args.max_classes,
                                           classify, args.test_episodes, args.likelihood_classification)
        print
        log.info('accuracy: %f' % accuracy)

        sys.exit()

    avg_pred_lb = np.zeros(episode_length)

    for epochs, lr in zip([250, 250, 250], [1e-3, 3e-4, 1e-4]):
    # for epochs, lr in zip([10], [1e-3]):
        for epoch in xrange(epochs):
            if num_epochs < done_epochs:
                num_epochs += 1
                continue

            epoch_started = time.time()
            total_batches = 24345 / batch_size / 10  # episode_length
            for batch in xrange(total_batches):
                pred_lb, i, _ = sess.run([train_pred_lb, global_step, train_op],
                                         feed_dict={learning_rate: lr})

                msg = '\repoch {0}, batch {1} '.format(epoch, i)
                avg_pred_lb += 0.01 * (pred_lb - avg_pred_lb)
                for t in xrange(episode_length):
                    assert not np.isnan(pred_lb[t])
                    msg += ' %.2f' % avg_pred_lb[t]
                sys.stdout.write(msg)
                sys.stdout.flush()
                if batch == total_batches-1:
                    print
                    log.info(msg)

            log.debug('time for epoch: %f', (time.time() - epoch_started))

            sess.run(increment_passed)
            if epoch % 5 == 0 and args.checkpoint is not None:
                saver.save(sess, args.checkpoint)

            if epoch % 5 == 0 and epoch > 0:
                test()

    # coord.request_stop()
    coord.join(data_threads)
