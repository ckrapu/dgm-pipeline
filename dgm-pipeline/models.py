import json
import utils
import builders as bld
import quality  as q
import numpy as np

import tensorflow as tf
import matplotlib.pyplot as plt

from functools import partial
from tqdm      import trange, tqdm
from os        import remove
from os.path   import exists


builder_mapping = {'conv_decoder':bld.conv_decoder,
                   'conv_encoder':bld.conv_encoder,
                   'test_decoder':bld.test_decoder,
                   'test_encoder':bld.test_encoder,
                   'resnet_decoder':bld.resnet_decoder,
                   'resnet_encoder':bld.resnet_encoder}

SPECS_DIR = '../model_specs/network_specs/'
VIZ_DIR   = '../data/visualizations/'  
SAVED_MODELS_DIR = '../data/saved_models/'

class GenerativeModel(tf.keras.Model):
    '''Common class for deep generative models'''
    def __init__(self,spec,dataset):
        super().__init__()

        self.spec = spec
        self.dataset=dataset
        self.latent_dim = spec['latent_dim']
        self.image_dims = spec['image_dims']

    def get_num_batches(self):
        '''
        Calculates the number of minibatches in a single epoch.
        '''
        return tf.data.experimental.cardinality(self.dataset).numpy()

    def create_generator(self):
        '''
        Initialize generative network either via reading
        a JSON spec or using a function to initialize it.
        '''
        # Create generator and fix the input size
        if 'json' in self.spec['generative_net']:
            with open(SPECS_DIR + self.spec['generative_net']) as gen_src:
                gen_spec = gen_src.read()
            gen_spec = utils.fix_batch_shape(gen_spec, [None,self.latent_dim])
            self.generative_net = tf.keras.models.model_from_json(gen_spec)
        else:
            network_builder = builder_mapping[self.spec['generative_net']]
            kw = self.spec['generative_net_kwargs']
            self.generative_net = network_builder(self.latent_dim, self.image_dims, **kw)
    
    def create_inference(self,output_shape=1):
        '''
        Initialize inference/decoder network either via reading
        a JSON spec or using a function to initialize it.
        '''
        if 'json' in self.spec['inference_net']:
            with open(SPECS_DIR + self.spec['inference_net']) as inf_src:
                inf_spec = inf_src.read()

            inf_spec = utils.fix_batch_shape(inf_spec, [None]+ list(self.image_dims))
            self.inference_net = tf.keras.models.model_from_json(inf_spec)
        else:
            network_builder = builder_mapping[self.spec['inference_net']]
            kw = self.spec['inference_net_kwargs']

            self.inference_net = network_builder(output_shape, self.image_dims, **kw)

    def load_pretrained(self, gen_path=None, inf_path=None):
        '''
        Use pretrained Keras model for either generative or inference
        network.
        '''
        if gen_path is not None:
            self.generative_net = tf.keras.models.load_model(gen_path)
        
        if inf_path is not None:
            self.inference_net = tf.keras.models.load_model(inf_path)

    def save(self, prefix, overwrite=True):
        '''
        Save networks to disk via Keras utilities.
        '''
        has_generator = hasattr(self, 'generative_net')
        has_inference = hasattr(self, 'inference_net')

        if has_inference:           
            inference_path = prefix + '_inference_net.h5'
            if exists(inference_path):
                remove(inference_path)
            self.inference_net.save(inference_path, overwrite=overwrite)

        if has_generator:
            generative_path = prefix + '_generative_net.h5'
            if exists(generative_path):
                remove(generative_path)
            self.generative_net.save(generative_path, overwrite=overwrite)

        if not (has_generator or has_inference):
            raise ValueError('No model object found for saving.')
    
    def plot_sample(self,n=36,nrows=6,ncols=12,plot_kwargs={},apply_sigmoid=False):
        '''
        Plot samples drawn from prior for generative model next to samples from
        the training data.
        '''
        x_synth = self.sample(n=n, apply_sigmoid=apply_sigmoid)
        x_true = self.sample_training(n=n)
        x = np.concatenate([x_synth, x_true])
        flat_x = utils.flatten_image_batch(x, nrows=nrows, ncols=ncols)
        fig = plt.figure(figsize=(8,3))
        ax = plt.imshow(flat_x, **plot_kwargs)
        plt.axis('off')
        plt.colorbar()

        return fig, ax

    def test_batch(self):
        if hasattr(self,'dataset'):
            iterator = self.dataset.as_numpy_iterator()
            return next(iterator)

        else:
            raise ValueError('Dataset has not been set for this model yet.')

    def create_masked_logp(self,batch,loss_elemwise_fn,loss_kwargs={},
                              final_activation_fn=None,dtype='float32',
                              temperature=1.):
        '''
        Applies masking to the logged posterior for a set of images
        according.
        '''

        # If the input is a Numpy masked array, use the mask
        # to figure out what values to include in logp
        if hasattr(batch, 'mask'):
            is_masked = batch.mask
            is_used   = tf.cast(1 - is_masked,dtype)
            raw_data = tf.cast(batch.data, dtype)
        else:
            is_used = 1.
            raw_data = tf.cast(batch, dtype)

        def logp(z):
            x = self.generative_net(z)
            
            if final_activation_fn is not None:
                x = final_activation_fn(x)

            loss_elemwise = loss_elemwise_fn(raw_data, x, **loss_kwargs)

            # The argument to reduce sum should have 4 dimensions
            loglike = -tf.reduce_sum(loss_elemwise * is_used, axis=[1,2,3])

            # We can use hot / cold posteriors by altering
            # the temperature value
            return temperature*loglike + self.log_prior_fn(z)

        return logp

    def inception_score(self, classifier_path,n=10000):
        '''
        Calculates the inception score for this model
        using an externally trained classifier.
        '''

        if not hasattr(self,'scores'):
            self.scores = {}

        xs = self.sample(n)
        iscore = qq.inception_score(classifier_path, xs)
        self.scores['inception_score'] = iscore
        return iscore

    def decode(self, z, apply_sigmoid=False):
        raw = self.generative_net(z)
        if apply_sigmoid:
            probs = tf.sigmoid(raw)
            return probs
        return raw

    def sample(self, z=None, n=100, prior=tf.random.normal, apply_sigmoid=False):
        if z is None:
            z = prior(shape=(n, self.latent_dim))
        x = self.decode(z, apply_sigmoid=apply_sigmoid)
        return x.numpy()

    def summary(self):
        self.inference_net.summary()
        self.generative_net.summary()

    def sample_training(self,n=36):
        gen = self.dataset.as_numpy_iterator()
        batch_size = self.dataset.element_spec.shape[0]
        n_batches = int(n/batch_size)+1
        return np.vstack([gen.next() for i in range(n_batches)])[0:n]
    
    def add_to_sample_history(self,n=36, apply_sigmoid=False):
        '''
        Add samples to record of samples from past epochs.
        '''
        if not hasattr(self,'sample_history'):
            self.sample_history = []
        self.sample_history.append(self.sample(n=n, apply_sigmoid=apply_sigmoid))


class GAN(GenerativeModel):
    '''
    Class for training and sampling from a generative adversarial network.
    This implementation uses Wasserstein loss with gradient penalty (WGAN-GP).
    '''
    def __init__(self, spec):
        super().__init__(spec)
        

        self.create_generator()
        self.create_inference(output_shape=1)

        # Assumes that batch of z variable will have shape
        # [batch_size X latent_dim]
        self.log_prior_fn = lambda z: -tf.reduce_sum(z**2,axis=-1)/2
    
        self.d_loss_fn, self.g_loss_fn = utils.get_wgan_losses_fn()

        self.G_optimizer = tf.keras.optimizers.Adam(learning_rate=spec['learning_rate'], beta_1=0.5)
        self.D_optimizer = tf.keras.optimizers.Adam(learning_rate=spec['learning_rate'], beta_1=0.5)

    @tf.function
    def train_generator(self):

        with tf.GradientTape() as t:
            z = tf.random.normal(shape=(self.spec['batch_size'], self.latent_dim ))
            x_fake = self.generative_net(z, training=True)
            x_fake_d_logit = self.inference_net(x_fake, training=True)
            G_loss = self.g_loss_fn(x_fake_d_logit)

        G_grad = t.gradient(G_loss, self.generative_net.trainable_variables)
        self.G_optimizer.apply_gradients(zip(G_grad, self.generative_net.trainable_variables))

        return {'g_loss': G_loss}

    @tf.function
    def train_discriminator(self,x_real):
        with tf.GradientTape() as t:
            z = tf.random.normal(shape=(self.spec['batch_size'], self.latent_dim))
            x_fake = self.generative_net(z, training=True)

            x_real_d_logit = self.inference_net(x_real, training=True)
            x_fake_d_logit = self.inference_net(x_fake, training=True)

            x_real_d_loss, x_fake_d_loss =  self.d_loss_fn(x_real_d_logit, x_fake_d_logit)
            gp = utils.gradient_penalty(partial(self.inference_net,
                                        training=True), x_real, x_fake)

            D_loss = (x_real_d_loss + x_fake_d_loss) + gp * self.spec['gradient_penalty']

        D_grad = t.gradient(D_loss, self.inference_net.trainable_variables)
        self.D_optimizer.apply_gradients(zip(D_grad, self.inference_net.trainable_variables))

        return {'d_loss': x_real_d_loss + x_fake_d_loss, 'gp': gp}

    def train(self, loss_update=100, epochs=None, plot_after_epoch=True):
        
        self.loss_history = []

        if epochs is None and 'epochs' in self.spec.keys():
            epochs = self.spec['epochs']
        else:
            raise ValueError('Provide a number of epochs to use via JSON specification or keyword argument.')

        for e in range(epochs):
            t = tqdm(enumerate(self.dataset),desc='Loss')
            for j,x_real in t:
                D_loss_dict = self.train_discriminator(x_real)

                if self.D_optimizer.iterations.numpy() % self.spec['gen_train_steps']== 0:
                    G_loss_dict = self.train_generator()

                if j % loss_update == 0 and j > self.spec['gen_train_steps']:
                    disc_loss = D_loss_dict['d_loss']
                    gp_loss = D_loss_dict['gp']
                    gen_loss = G_loss_dict['g_loss']
                    loss_str = f'Loss - Discriminator: {disc_loss}, Generator: {gen_loss}, Gradient Penalty: {gp_loss}'
                    t.set_description(loss_str)
                    self.loss_history.append([disc_loss, gp_loss, gen_loss])
            if plot_after_epoch:
                self.plot_sample()
            self.add_to_sample_history()

class VAE(GenerativeModel):
    '''Class for training and sampling from a variational autoencoder'''
    def __init__(self, spec, dataset):
        super().__init__(spec, dataset)

        self.create_generator()
        self.create_inference(output_shape=self.latent_dim*2)

        # Assumes that batch of z variable will have shape
        # [batch_size X latent_dim]
        self.log_prior_fn = lambda z: -tf.reduce_sum(z**2,axis=-1)/2


        # TODO: remove this hack for using if-else cases to select
        # the optimizer
        settings = self.spec['opt_kwargs']

        # Optimizer may be initialized from earlier runs
        if self.spec['optimizer'] == 'adam':
            self.optimizer = tf.keras.optimizers.Adam(**settings)
        else:
            raise NotImplementedError('Other optimizers are not \
                                    yet supported.')

        # Control representation capacity per Burgess et al. 2018
        # 'Understanding disentangling in beta-VAE'
        if 'vae_beta' in self.spec.keys():
            self.beta = self.spec['vae_beta']
        else:
            self.beta = 1.
        
        loglike_type = self.spec['likelihood']

        if loglike_type == 'bernoulli':
            self.loglike = cross_ent_loss

        elif loglike_type =='continuous_bernoulli':
            self.loglike = cb_loss

        elif loglike_type == 'normal':
            # Enables the error sd to be variable and
            # learned by the data
            if self.spec['error_trainable']:
                self.error_sd = tf.Variable(0.1)
            else:
                self.error_sd = 0.1
            self.loglike = partial(square_loss, sd=self.error_sd)
            
        else:
            raise ValueError('Likelihood argument not understood. \
                Try one of "bernoulli", "continuous_bernoulli" or "normal".')

        self.loss_fn = partial(vae_loss, loglike=self.loglike)
        self.set_beta_schedule(beta_max=self.beta)

        self.loss_history = np.asarray([])

        self.n_batches = self.get_num_batches()

    def encode(self, x):
        mean, logvar = tf.split(self.inference_net(x), num_or_size_splits=2, axis=1)
        return mean, logvar

    def reparameterize(self, mean, logvar):
        eps = tf.random.normal(shape=mean.shape)
        return eps * tf.exp(logvar * .5) + mean

    def set_beta_schedule(self,beta_max=1.,default_cycle_length=5):
        n_epochs = self.spec['epochs']

        if 'beta_cycle_length' in self.spec.keys():
            cycle_length = self.spec['beta_cycle_length']
        else:
            cycle_length = default_cycle_length
            
        if 'beta_schedule' in self.spec.keys():
            schedule = self.spec['beta_schedule']
            
            if schedule == 'linear':
                self.beta_schedule = np.linspace(0,beta_max,n_epochs)
            elif 'cyclic' in schedule:
                ncycles = max(int(n_epochs/cycle_length),1)
                self.beta_schedule = np.tile(np.linspace(0,1,cycle_length),ncycles)*beta_max
            elif schedule == 'constant':
                self.beta_schedule = np.ones(n_epochs) * beta_max

    @staticmethod
    @tf.function
    def compute_apply_gradients(model, optimizer, x, loss_fn, beta=1.):
        with tf.GradientTape() as tape:
            loss = loss_fn(model, x, beta=beta)
            gradients = tape.gradient(loss, model.trainable_variables)
        optimizer.apply_gradients(zip(gradients, model.trainable_variables))
        return loss

    def train(self, loss_update=100, epochs=None, plot_after_epoch=True,
              save_interval=2):

        if epochs is None and 'epochs' in self.spec.keys():
            epochs = self.spec['epochs']
        else:
            raise ValueError('Provide a number of epochs to use via JSON specification or keyword argument.')
        
        t = trange(epochs,desc='Loss')

        for current_epoch in t:
     
            if hasattr(self,'beta_schedule'):
                self.beta_current = tf.cast(self.beta_schedule[current_epoch],dtype='float32')
            else:
                self.beta_current = tf.cast(self.beta,dtype='float32')

            epoch_loss = self.train_single_epoch()
            final_loss = epoch_loss[-1]

            t.set_description('Loss=%g' % final_loss)
            self.loss_history = np.concatenate([self.loss_history,epoch_loss])

            apply_sigmoid = 'bernoulli' in self.spec['likelihood']
            if plot_after_epoch:
                self.plot_sample(apply_sigmoid=apply_sigmoid)

            if current_epoch % save_interval == 0:
                self.save(SAVED_MODELS_DIR+self.spec['name'])

            self.add_to_sample_history(apply_sigmoid=apply_sigmoid)

    
    def train_single_epoch(self):
        loss_history = np.zeros(self.n_batches)
        for i, minibatch in enumerate(self.dataset):               
            loss = self.compute_apply_gradients(self, self.optimizer, 
                                                minibatch, self.loss_fn, 
                                                beta=self.beta_current)
            loss_history[i] = loss
        return loss_history

@tf.function
def square_loss(x_pred, x_true, sd=1, axis=[1,2,3,]):
    error = square_loss_elem(x_pred,x_true,sd=1)
    return -tf.reduce_sum(error,axis=axis)   

def square_loss_elem(x_pred,x_true,sd=1):
    return (x_pred-x_true)**2 / (2*sd**2)

@tf.function
def cross_ent_loss(x_logit, x_label, axis=[1,2,3]):
    cross_ent = tf.nn.sigmoid_cross_entropy_with_logits(logits=x_logit, labels=x_label)
    loss = -tf.reduce_sum(cross_ent, axis=axis)
    return loss

@tf.function
def cb_loss(x_logit, x_true, axis=[1,2,3]):
    '''
    Continuous Bernoulli loss per Loaiza-Ganem and Cunningham 2019.
    '''
    bce = wrapped_cross_ent(x_true, x_logit)
    x_sigmoid = tf.math.sigmoid(x_logit)
    logc = log_cb_constant(x_sigmoid)
    loss = -tf.reduce_sum(bce, axis=axis) - logc
    return loss

@tf.function
def log_cb_constant(x, eps=1e-5):
    '''
    Calculates log of the normalization constant
    for the continuous Bernoulli likelihood.
    '''
    x = tf.clip_by_value(x, eps, 1-eps)
    mask = tf.math.greater_equal(tf.math.abs(x - 0.5),(eps))
    far   = x[mask]
    close = x[~mask]
    far_values =  tf.math.log( (tf.math.log(1. - far) - tf.math.log(far))/(1. - 2. * far) )
    close_values = tf.math.log(2.) + tf.math.log(1. + tf.math.pow( 1. - 2. * close, 2)/3. )
    return tf.reduce_sum(far_values) + tf.reduce_sum(close_values)

@tf.function
def vae_loss(model, x, loglike, beta=1.):
    mean, logvar = model.encode(x)
    z = model.reparameterize(mean, logvar)
    x_pred = model.decode(z)
    logpx_z = loglike(x_pred, x)
    logpz = utils.log_normal_pdf(z, 0., 0.)
    logqz_x = utils.log_normal_pdf(z, mean, logvar)
    kld = logqz_x - logpz
    return -tf.reduce_mean(logpx_z - beta * kld)

@tf.function
def vae_cross_ent_loss(model, x, beta=1.):
    mean, logvar = model.encode(x)
    z = model.reparameterize(mean, logvar)
    x_logit = model.decode(z)
    logpx_z = cross_ent_loss(x_logit, x)
    logpz = utils.log_normal_pdf(z, 0., 0.)
    logqz_x = utils.log_normal_pdf(z, mean, logvar)
    kld = logqz_x - logpz
    return -tf.reduce_mean(logpx_z - beta*kld)

@tf.function
def wrapped_cross_ent(true, pred):
    return tf.nn.sigmoid_cross_entropy_with_logits(logits=pred,labels=true)


