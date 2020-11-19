
import sys, os  #, traceback, ipdb
#os.environ["CUDA_VISIBLE_DEVICES"]="0"
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributions as distrib

import numpy as np
import matplotlib.pyplot as plt

import omnifig as fig

try:
	import umap, shap
	import umap.plot
	import gpumap
except ImportError:
	print('WARNING: umap not found')
from sklearn.decomposition import PCA

import foundation as fd
from foundation import models
from foundation import util
# from foundation import train as trn

# if 'FOUNDATION_RUN_MODE' in os.environ and os.environ['FOUNDATION_RUN_MODE'] == 'jupyter':
# 	from tqdm import tqdm_notebook as tqdm
# else:
from tqdm import tqdm

import visualizations as viz_util
# import encoders
# import pointnets
import adain
import decoders
import transfer

MY_PATH = os.path.dirname(os.path.abspath(__file__))

@fig.Component('run')
class SAE_Run(fd.op.Torch):

	def _gen_name(self, A):
		
		model = A.pull('info.model_type', '<>model._model_type', '<>model._type', None, silent=True)
		data = A.pull('info.dataset_type', '<>dataset.name', '<>dataset._type', None, silent=True)
		
		name = f'{model}_{data}'
		
		arch = A.pull('info.arch', None, silent=True)
		if arch is not None:
			name = f'{name}_{arch}'
		
		extra = A.pull('info.extra', None, silent=True)
		if extra is not None:
			name = f'{name}_{extra}'
		
		return name


# region Algorithms

@fig.Component('ae')
class AutoEncoder(fd.Generative, fd.Encodable, fd.Decodable, fd.Regularizable, fd.Full_Model):
	def __init__(self, A):

		encoder = A.pull('encoder')
		decoder = A.pull('decoder')

		criterion = A.pull('criterion', 'bce') # {'_type':'criterion', 'name':'bce', 'kwargs':{'reduction':'sum'}}

		reg_wt = A.pull('reg_wt', 0)
		reg = A.pull('reg', 'L2')

		viz_gen = A.pull('viz_gen', False)

		hparams = {'reg_type': str(reg),}

		super().__init__(encoder.din, decoder.dout)

		self.enc = encoder
		self.dec = decoder
		
		self.latent_dim = self.enc.dout

		self.criterion = util.get_loss_type(criterion, reduction='sum')
		self.reg_wt = reg_wt
		self.reg_fn = util.get_regularization(reg, reduction='sum')
		if self.reg_wt > 0:
			self.stats.new('reg')
		self.stats.new('rec_loss')

		self.register_buffer('_q', None, save=True)
		self.register_cache('_real', None)
		self.register_cache('_rec', None)
		self.viz_gen = viz_gen
		
		self._hparams = hparams

	def get_hparams(self):
		
		h = self.enc.get_hparams()
		h.update(self.dec.get_hparams())
		
		h['reg_wt'] = self.reg_wt
		
		h.update(self._hparams)
		
		return h

	def _evaluate(self, loader, logger=None, A=None, run=None):
		
		inline = A.pull('inline', False)
		
		# region Prep
		
		results = {}
		
		device = A.pull('device', 'cpu')
		
		self.stats.reset()
		batches = iter(loader)
		total = 0
		
		batch = next(batches)
		batch = util.to(batch, device)
		total += batch.size(0)
		
		with torch.no_grad():
			out = self.test(batch)
		
		if isinstance(self, fd.Visualizable):
			self._visualize(out, logger)
		
		results['out'] = out
		
		for batch in loader:  # complete loader for stats
			batch = util.to(batch, device)
			total += batch.size(0)
			with torch.no_grad():
				self.test(batch)
		
		results['stats'] = self.stats.export()
		display = self.stats.avgs()  # if smooths else stats.avgs()
		for k, v in display.items():
			logger.add('scalar', k, v)
		results['stats_num'] = total
		
		# region fid
		
		dataset = loader.get_dataset()
		batch_size = loader.get_batch_size()
		
		print(f'data: {len(dataset)}, loader: {len(loader)}')
		
		skip_fid = A.pull('skip-fid', False)
		
		if not skip_fid:
			fid_dim = A.pull('fid-dim', 2048)
			
			n_samples = max(10000, min(len(dataset), 50000))
			n_samples = A.pull('n-samples', n_samples)
			
			if 'inception_model' not in self.volatile:
				self.volatile.inception_model = fd.eval.fid.load_inception_model(dim=fid_dim, device=device)
				self.volatile.ds_stats = dataset.get_fid_stats('train', fid_dim)
			inception_model = self.volatile.inception_model
			ds_stats = self.volatile.ds_stats
			

			# hyb fid
			gen_fn = self.generate_hybrid
			
			m, s = fd.eval.fid.compute_inception_stat(gen_fn, inception=inception_model,
			                                          batch_size=batch_size, n_samples=n_samples,
			                                          pbar=tqdm if inline else None)
			results['hyb_fid_stats'] = [m, s]
			
			if ds_stats is not None:
				score = fd.eval.fid.compute_frechet_distance(m, s, *ds_stats)
				results['hyb_fid'] = score
				
				logger.add('scalar', 'fid-hyb', score)
				
			# rec fid
			def make_gen_fn():
				myloader = util.make_infinite(loader)
				
				def gen_fn(N):
					# assert N == batch_size, '{} vs {}'.format(N, batch_size)
					x = util.to(myloader.demand(N), device)[0]
					return self(x)
				
				return gen_fn
			
			m, s = fd.eval.fid.compute_inception_stat(make_gen_fn(), inception=inception_model,
			                                          batch_size=batch_size, n_samples=n_samples,
			                                          pbar=tqdm if 'inline' in A and A.inline else None)
			results['rec_fid_stats'] = [m, s]
			
			if ds_stats is not None:
				score = fd.eval.fid.compute_frechet_distance(m, s, *ds_stats)
				results['rec_fid'] = score
				
				logger.add('scalar', 'fid-rec', score)
		
		# endregion
		
		logger.flush()
		
		return results

	# def visualize(self, info, logger):
	# 	if not self.training or self._viz_counter % 2

	def _visualize(self, info, logger):

		if isinstance(self.enc, fd.Visualizable):
			self.enc.visualize(info, logger)
		if isinstance(self.dec, fd.Visualizable):
			self.dec.visualize(info, logger)

		q = None
		if 'latent' in info and info.latent is not None:
			q = info.latent.loc if isinstance(info.latent, distrib.Distribution) else info.latent

			shape = q.size()
			if len(shape) > 1 and np.product(shape) > 0:
				try:
					logger.add('histogram', 'latent-norm', q.norm(p=2, dim=-1))
					logger.add('histogram', 'latent-std', q.std(dim=0))
					if isinstance(info.latent, distrib.Distribution):
						logger.add('histogram', 'logstd-hist', info.latent.scale.add(1e-5).log().mean(dim=0))
				except ValueError:
					print('\n\n\nWARNING: histogram just failed\n')
					print(q.shape, q.norm(p=2, dim=-1).shape)

		B, C, H, W = info.original.shape
		N = min(B, 8)

		if 'reconstruction' in info:
			viz_x, viz_rec = info.original[:N], info.reconstruction[:N]

			recs = torch.cat([viz_x, viz_rec], 0)
			logger.add('images', 'rec', self._img_size_limiter(recs))
		elif self._rec is not None:
			viz_x, viz_rec = self._real[:N], self._rec[:N]

			recs = torch.cat([viz_x, viz_rec], 0)
			logger.add('images', 'rec', self._img_size_limiter(recs))

		if self.viz_gen or not self.training:
			viz_gen = self.generate_hybrid(2*N)
			logger.add('images', 'gen-hyb', self._img_size_limiter(viz_gen))

		if not self.training: # expensive visualizations
			
			n = 16
			steps = 20
			ntrav = 1
			
			if q is not None and len(q) >= n:
				fig, (lax, iax) = plt.subplots(2, figsize=(2*min(q.size(1)//20+1,3)+2,3))
				
				viz_util.viz_latent(q, figax=(fig, lax), )
				
				Q = q[:n]
				
				vecs = viz_util.get_traversal_vecs(Q, steps=steps,
                      mnmx=(Q.min(0)[0].unsqueeze(-1), Q.max(0)[0].unsqueeze(-1))).contiguous()
				# deltas = torch.diagonal(vecs, dim1=-3, dim2=-1)
				
				walks = viz_util.get_traversals(vecs, self.decode, device=self.device).cpu()
				diffs = viz_util.compute_diffs(walks)
				
				info.diffs = diffs
				
				viz_util.viz_interventions(diffs, figax=(fig, iax))
				

				# fig.tight_layout()
				border, between = 0.02, 0.01
				plt.subplots_adjust(wspace=between, hspace=between,
										left=5*border, right=1 - border, bottom=border, top=1 - border)
				
				logger.add('figure', 'distrib', fig)
				
				full = walks[1:1+ntrav]
				del walks
				
				tH, tW = util.calc_tiling(full.size(1), prefer_tall=True)
				B, N, S, C, H, W = full.shape
				
				# if tH*H > 200: # limit total image size
				# 	pass
				
				full = full.view(B, tH, tW, S, C, H, W)
				full = full.permute(0, 3, 4, 1, 5, 2, 6).contiguous().view(B, S, C, tH * H, tW * W)
				
				logger.add('video', 'traversals', full, fps=12)
			
			
			else:
				print('WARNING: visualizing traversals failed')
				

			logger.flush()

	def _img_size_limiter(self, imgs):
		H, W = imgs.shape[-2:]

		if H*W < 2e4: # allows upto around 128x128
			return imgs

		imgs = F.interpolate(imgs, size=(128,128))
		return imgs

	def _step(self, batch, out=None):
		if out is None:
			out = util.TensorDict()

		x = batch[0]
		B = x.size(0)

		out.original = x

		rec, q = self(x, ret_q=True)
		out.latent = q
		out.reconstruction = rec

		self._rec, self._real = rec.detach(), x.detach()

		loss = self.criterion(rec, x) / B
		out.rec_loss = loss
		self.stats.update('rec_loss', loss)

		if self.reg_wt > 0:
			reg_loss = self.regularize(q)
			self.stats.update('reg', reg_loss)
			out.reg_loss = reg_loss
			loss += self.reg_wt * reg_loss

		out.loss = loss

		if self.train_me():
			self._q = q.loc.detach() if isinstance(q, distrib.Normal) else q.detach()

			self.optim.zero_grad()
			loss.backward()
			self.optim.step()

		return out

	def hybridize(self, q=None):

		if q is None:
			q = self._q

		return util.shuffle_dim(q)

	def generate(self, N=1):
		return self.generate_hybrid(N)

	def generate_hybrid(self, N=1):

		if self._q is None:
			raise NotImplementedError

		q = torch.cat([self._q]*(N//len(self._q)+1))

		hyb = self.hybridize(q)[:N]

		return self.decode(hyb)

	def encode(self, x):
		return self.enc(x)

	def decode(self, q):
		return self.dec(q)

	def forward(self, x, ret_q=False):

		q = self.encode(x)
		rec = self.decode(q)

		if ret_q:
			return rec, q
		return rec

	def regularize(self, q, p=None):
		B = q.size(0)
		mag = self.reg_fn(q)
		return mag / B

@fig.AutoModifier('teval')
class Transfer_Eval(fd.Full_Model):
	
	def __init__(self):
		raise NotImplementedError
	
	def prep(self, *datasets):
		dataset = datasets[0]
		assert isinstance(dataset, transfer.Multi_Dataset), f'not transfer setting: {dataset}'
		super().prep(*datasets)
	
	def _evaluate(self, info):
		
		A = info['A']
		
		valdata = info['datasets'][-1]
		testdata = None
		if 'testset' in info:
			testdata = info['testset']
		
		groups = {name: [name] for name in valdata.folds}
		full = '_full' if 'full' in groups else 'full'
		groups[full] = list(groups)
		groups = A.pull('groups', groups)
		
		print('Will run the full evaluation {} times (once per group)'.format(len(groups)))
		
		logger = info['logger']
		logger_id = info['identifier']
		
		results = {}
		
		for name, folds in groups.items():
			
			print('Evaluation for group {}: {}'.format(name, ', '.join(folds)))
			
			if logger is not None:
				logger.set_tag_format('{}-{}/{}'.format(logger_id, name, '{}'))
		
			valdata.set_full(*folds)
			if testdata is not None:
				testdata.set_full(*folds)
			
			results[name] = super()._evaluate(info)
			
		return results



class Prior_Autoencoder(AutoEncoder):
	
	def sample_prior(self, N=1):
		return torch.randn(N, self.latent_dim, device=self.device)
	
	def generate(self, N=1):
		return self.generate_prior(N)
		
	def generate_prior(self, N=1):
		q = self.sample_prior(N)
		return self.decode(q)
	
	def _visualize(self, info, logger):
		super()._visualize(info, logger)
		
		if self._viz_counter % 2 == 0 or not self.training:
			# q = None
			# if 'latent' in info and info.latent is not None:
			# 	q = info.latent.loc if isinstance(info.latent, distrib.Distribution) else info.latent
			#
			# 	shape = q.size()
			# 	if len(shape) > 1 and np.product(shape) > 0:
			# 		try:
			# 			logger.add('histogram', 'latent-norm', q.norm(p=2, dim=-1))
			# 			logger.add('histogram', 'latent-std', q.std(dim=0))
			# 			if isinstance(info.latent, distrib.Distribution):
			# 				logger.add('histogram', 'logstd-hist', info.latent.scale.add(1e-5).log().mean(dim=0))
			# 		except ValueError:
			# 			print('\n\n\nWARNING: histogram just failed\n')
			# 			print(q.shape, q.norm(p=2, dim=-1).shape)
			#
			B, C, H, W = info.original.shape
			N = min(B, 8)
			
			if self.viz_gen or not self.training:
				viz_gen = self.generate_prior(2 * N)
				logger.add('images', 'gen-prior', self._img_size_limiter(viz_gen))
		
		
	def _evaluate(self, loader, logger=None, A=None, run=None):
		
		results = super()._evaluate(loader, logger=logger, A=A, run=run)
		
		inline = A.pull('inline', False)
		device = A.pull('device', 'cpu')
		
		dataset = loader.get_dataset()
		batch_size = loader.get_batch_size()
		
		print(f'data: {len(dataset)}, loader: {len(loader)}')
		
		skip_fid = A.pull('skip-fid', False)
		
		if not skip_fid:
			fid_dim = A.pull('fid-dim', 2048)
			
			n_samples = max(10000, min(len(dataset), 50000))
			n_samples = A.pull('n-samples', n_samples)
			
			if 'inception_model' not in self.volatile:
				self.volatile.inception_model = fd.eval.fid.load_inception_model(dim=fid_dim, device=device)
				self.volatile.ds_stats = dataset.get_fid_stats('train', fid_dim)
			inception_model = self.volatile.inception_model
			ds_stats = self.volatile.ds_stats
			
			gen_fn = self.generate_hybrid
			
			m, s = fd.eval.fid.compute_inception_stat(gen_fn, inception=inception_model,
			                                          batch_size=batch_size, n_samples=n_samples,
			                                          pbar=tqdm if inline else None)
			results['prior_fid_stats'] = [m, s]
			
			if ds_stats is not None:
				score = fd.eval.fid.compute_frechet_distance(m, s, *ds_stats)
				results['prior_fid'] = score
				
				logger.add('scalar', 'fid-prior', score)
		
		return results

@fig.Component('vae')
class VAE(Prior_Autoencoder):
	def __init__(self, A, norm_mod=None):
		
		if norm_mod is None:
			norm_mod = models.Normal_Distrib_Model

		reg_wt = A.pull('reg_wt', None, silent=True)
		assert reg_wt is not None and reg_wt > 0, 'not a vae without regularization'

		A.push('reg', None, silent=True)

		super().__init__(A)
		
		self._hparams['reg_type'] = 'KL'
		self._hparams['enc_type'] = 'VAE'

		if not isinstance(self.enc, norm_mod):
			print('WARNING: encoder apparently does not output a normal distribution')
		# assert isinstance(self.enc, models.Normal), 'encoder must output a normal distrib'

	def regularize(self, q):
		return util.standard_kl(q).sum().div(q.loc.size(0))

	def decode(self, q):
		if isinstance(q, distrib.Distribution):
			q = q.rsample()
		return super().decode(q)

@fig.Component('wae')
class WAE(Prior_Autoencoder):
	
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._hparams['reg_type'] = 'W'
	
	def regularize(self, q, p=None):
		if p is None:
			p = self.sample_prior(q.size(0))
		return util.MMD(p, q)

@fig.Component('swae')
class Slice_WAE(Prior_Autoencoder):
	def __init__(self, A):
		slices = A.pull('slices', '<>latent_dim')

		super().__init__(A)

		self.slices = slices
		
		self._hparams['reg_type'] = 'SW'
		self._hparams['slices'] = slices

	def sample_slices(self, N=None): # sampled D dim unit vectors
		if N is None:
			N = self.slices

		return F.normalize(torch.randn(self.latent_dim, N, device=self.device), p=2, dim=0)

	def regularize(self, q, p=None):

		s = self.sample_slices() # D, S

		qd = q @ s
		qd = qd.sort(0)[0]

		if p is None:
			p = self.sample_prior(q.size(0))
		pd = p @ s
		pd = pd.sort(0)[0]

		return (qd - pd).abs().mean()


class Cost_Aware(Prior_Autoencoder):
	def __init__(self, A):
		
		reg_imp_p = A.pull('reg_imp_p', 1)
		
		reg_imp_wt = A.pull('reg_imp_wt', 0.5)
		reg_prior_wt = A.pull('reg_prior_wt', 1)
		
		init_imp_mu = A.pull('init_imp_mu', 0)
		init_imp_std = A.pull('init_imp_std', 1)
		
		weigh_distances = A.pull('weigh_distances', False)
		
		imp_noise = A.pull('imp_noise', 0)
		
		super().__init__(A)
		
		self.register_cache('_rew_q')
		
		self.stats.new('imp', 'reg_imp', 'reg_prior')
		
		self.importance = nn.Parameter(init_imp_std*torch.randn(self.latent_dim) + init_imp_mu,
		                               requires_grad=True)

		self.reg_imp_p = reg_imp_p
		
		self.reg_imp_wt = reg_imp_wt
		self.reg_prior_wt = reg_prior_wt
		
		self.weigh_distances = weigh_distances
		self.imp_noise = imp_noise

	def get_importance(self, noisy=False):
		imp = self.importance
		if noisy and self.imp_noise > 0:
			imp = imp + torch.randn_like(imp).mul(self.imp_noise)
		return F.sigmoid(imp)

	def _visualize(self, info, logger):

		if self._viz_counter % 2 == 0 or not self.training:
			logger.add('histogram', 'imp_hist', self.importance.clamp(min=-5, max=5))
			logger.add('text', 'imp_str', '[{}]'.format(', '.join('{:2.3f}'.format(i.item()) for i in self.importance)))

		super()._visualize(info, logger)

	def regularize(self, q):
		
		v = self.get_importance()
		self.stats.update('imp', v.sum())
		
		# reg_imp = v.norm(p=self.reg_imp_p)
		# reg_imp = F.elu(self.importance).sum()
		reg_imp = v.pow(self.reg_imp_p).sum()
		self.stats.update('reg_imp', reg_imp)
		
		if self._raw_q is not None:
			q = self._raw_q
			self._raw_q = None
		
		p = self.sample_prior(q.size(0))
		if self.weigh_distances:
			q = q * v.unsqueeze(0).detach()
			p = p * v.unsqueeze(0).detach()
		
		reg_prior = super().regularize(q, p)
		self.stats.update('reg_prior', reg_prior)
		
		return self.reg_imp_wt * reg_imp + self.reg_prior_wt * reg_prior


@fig.Component('cae')
class Det_Cost_Aware(Cost_Aware):
	def encode(self, x):
		q = super().encode(x)
		B, D = q.size()
		
		self._raw_q = q
		
		v = self.get_importance(noisy=True).expand(B, D)
		p = self.sample_prior(B)
		
		q = v * q + (1 - v) * p
		# q = q + (1 - v) * p
		return q

class Sto_Cost_Aware(Cost_Aware):
	def encode(self, x):
		q = super().encode(x)
		return self.as_normal(q)
	
	def as_normal(self, q):
		std = self.get_importance(noisy=True).expand(*q.size())
		return distrib.Normal(loc=q, scale=std)

@fig.Component('cwae')
class Cost_Aware_WAE(Det_Cost_Aware, WAE):
	pass

@fig.Component('cswae')
class Cost_Aware_SWAE(Det_Cost_Aware, Slice_WAE):
	pass

@fig.Component('cvae')
class Cost_VAE(Sto_Cost_Aware, VAE):
	pass


@fig.AutoModifier('fixed-std')
class Fixed_Std(fd.Visualizable, fd.Model):
	def __init__(self, A, latent_dim=None):
		
		if latent_dim is None:
			latent_dim = A.pull('latent_dim', '<>dout')
		
		min_log_std = A.pull('min_log_std', None)
		
		super().__init__(A)
		
		self.log_std = nn.Parameter(torch.randn(latent_dim)*0.1, requires_grad=True)
		
		self.min_log_std = min_log_std
		self.latent_dim = latent_dim
		
	def get_hparams(self):
		return {'std_type': 'fixed'}
		
	def _visualize(self, info, logger):

		try:
			super()._visualize(info, logger)
		except NotImplementedError:
			pass


		pass
	
	def forward(self, *args, **kwargs):
		
		mu = super().forward(*args, **kwargs)
		logsigma = self.log_std
		
		if self.min_log_std is not None:
			logsigma = logsigma.clamp(min=self.min_log_std)

		return distrib.Normal(loc=mu, scale=logsigma.exp())
		




# endregion


# region Architectures


@fig.Component('extraction-enc')
class UMAP_Encoder(fd.Encodable, fd.Schedulable, fd.Model):

	def __init__(self, A):

		in_shape = A.pull('in_shape', '<>din')
		latent_dim = A.pull('latent_dim', '<>dout')
		feature_dim = A.pull('feature_dim', '<>latent_dim')

		transform = A.pull('transform', None)

		alg = A.pull('alg', 'umap')

		kwargs = {
			'n_components': feature_dim,
		}

		if alg == 'umap':

			extraction_cls = gpumap.GPUMAP

			kwargs['random_state'] = A.pull('random_state', '<>seed')
			kwargs['min_dist'] = A.pull('min_dist', 0.1)
			kwargs['n_neighbors'] = A.pull('neighbors', 15)

		elif alg == 'pca':
			extraction_cls = PCA

		else:
			raise Exception(f'unknown alg: {alg}')

		extractor = extraction_cls(**kwargs)

		if 'net' in A:
			A.net.din = feature_dim
			A.net.dout = latent_dim

		net = A.pull('net', None)

		training_limit = A.pull('training_limit', None)

		super().__init__(din=in_shape, dout=feature_dim if net is None else latent_dim)

		self.training_limit = training_limit

		self.transformer = transform

		self.alg = alg
		self.extractor = extractor

		self.net = net

		# self.set_optim(A)
		# self.set_scheduler(A)

	def _resize(self, x):
		N, C, H, W = x.shapes

		if H >= 64:
			return x[:, :, ::2, ::2].reshape(N, -1)
		return x.reshape(N, -1)

	def prep(self, traindata, *other):

		samples = traindata.get_raw_data().float()

		if self.training_limit is not None:
			samples = samples[:self.training_limit]

		samples = self._reformat(samples)

		print(f'Training a {self.alg} feature extractor to extract {self.extractor.n_components} '
		      f'features from an input {samples.shape}')


		# fit estimator
		self.extractor.fit(samples)

		print('Feature extraction complete')

	def encode(self, x):
		return self(x)

	def transform(self, x):

		device = x.device
		x = self._reformat(x)

		q = self.extractor.transform(x)
		q = torch.from_numpy(q).to(device)

		return q

	def _reformat(self, x):
		x = x.cpu().numpy()

		if self.transformer is not None:
			x = self.transformer(x)
		else:
			x = self._resize(x)

		return x

	def forward(self, x):

		q = self.transform(x)

		if self.net is None:
			return q
		return self.net(q)

@fig.Component('ladder-enc')
class Ladder_Encoder(fd.Encodable, fd.Schedulable, fd.Model):
	def __init__(self, A):
		
		if 'latent_dim' in A:
			A.latent_dim = None
	
		layers = A.pull('layers')
		csizes = layers._conv_shapes[1:]
		din = layers.din
		rung_dims = A.pull('rung_dims')
		
		reverse_order = A.pull('reverse_order', False)
		
		layer_key = A.pull('layer_key', None)
		if layer_key is not None:
			layers = getattr(layers, layer_key, layers)
		
		try:
			len(rung_dims)
		except TypeError:
			rung_dims = [rung_dims]*len(layers)

		ret_normal = isinstance(self, models.Normal_Distrib_Model)
		if ret_normal:
			rung_dims = [(2*r if r is not None else r) for r in rung_dims]
		
		latent_dim = sum(rung for rung in rung_dims if rung is not None)
		
		assert len(layers) == len(rung_dims)
		assert latent_dim > 0, 'no latent dim'
		
		super().__init__(din, latent_dim)
		
		create_rung = A.pull('rungs')
		rungs = []
		
		for i, (rdim, cdin) in enumerate(zip(rung_dims, csizes)):
			
			if rdim is None:
				rungs.append(None)
			else:
				nxt = create_rung.current()
				nxt.din = cdin
				nxt.dout = rdim
				rung = next(create_rung)
				rungs.append(rung)
		
		while rungs[-1] is None:
			rungs.pop()
			layers.pop()
		
		self.ret_distrib = ret_normal
		self.rung_dims = rung_dims
		self.layers = nn.ModuleList(layers)
		self.rungs = nn.ModuleList(rungs)
		self.reverse_order = reverse_order
	
	def forward(self, x):
		
		qs = []
		
		B = x.size(0)
		
		c = x
		for l, r in zip(self.layers, self.rungs):
			c = l(c)
			if r is not None:
				q = r(c)
				qs.append(q.view(B,2,-1) if self.ret_distrib else q)
		
		if self.reverse_order:
			qs = reversed(qs)
		
		q = torch.cat(qs,-1)
		if self.ret_distrib:
			q = q.view(B, -1)
		return q

@fig.Component('dislib-enc')
class Disentanglement_lib_Encoder(fd.Encodable, fd.Schedulable, fd.Model):
	def __init__(self, A):

		in_shape = A.pull('in_shape', '<>din')
		latent_dim = A.pull('latent_dim', '<>dout')

		nonlin = A.pull('nonlin', 'relu')

		C, H, W = in_shape

		assert (H,W) in {(64,64), (128,128)}, f'not a valid input size: {(H,W)}'

		net_type = A.pull('net_type', 'conv')

		assert net_type in {'conv', 'fc'}, f'unknown type: {net_type}'

		super().__init__(din=in_shape, dout=latent_dim)

		if net_type == 'conv':

			channels = [32,32,32,64,64]
			kernels = [4,4,4,2,2]
			strides = [2,2,2,2,2]

			if H == 64:
				channels = channels[1:]
				kernels = kernels[1:]
				strides = strides[1:]

			shapes, settings = models.plan_conv(in_shape, channels=channels, kernels=kernels, strides=strides)

			out_shape = shapes[-1]

			self.conv = nn.Sequential(*models.build_conv_layers(settings, nonlin=nonlin, out_nonlin=nonlin,
			                                                   pool_type=None, norm_type=None))

			self.net = models.make_MLP(out_shape, latent_dim, hidden=[256, ], nonlin=nonlin)

		else:

			self.net = models.make_MLP(in_shape, latent_dim, hidden=[1200, 1200], nonlin=nonlin)

		self.uses_conv = net_type == 'conv'

		# self.set_optim(A)
		# self.set_scheduler(A)

	def forward(self, x):
		c = self.conv(x) if self.uses_conv else x
		q = self.net(c)
		return q

	def encode(self, x):
		return self(x)


@fig.Component('dislib-dec')
class Disentanglement_lib_Decoder(fd.Decodable, fd.Schedulable, fd.Model):
	def __init__(self, A):

		latent_dim = A.pull('latent_dim', '<>din')
		out_shape = A.pull('out_shape', '<>dout')

		nonlin = A.pull('nonlin', 'relu')

		C, H, W = out_shape

		assert (H, W) in {(64, 64), (128, 128)}, f'not a valid output size: {(H, W)}'

		net_type = A.pull('net_type', 'conv')

		assert net_type in {'conv', 'fc'}, f'unknown type: {net_type}'

		super().__init__(din=latent_dim, dout=out_shape)

		if net_type == 'conv':

			channels = [64, 64, 32, 32, 32]
			kernels = [4, 4, 4, 4, 4]
			strides = [2, 2, 2, 2, 2]

			if H == 64:
				channels = channels[:-1]
				kernels = kernels[:-1]
				strides = strides[:-1]

			shapes, settings = models.plan_deconv(out_shape, channels=channels, kernels=kernels, strides=strides)

			in_shape = shapes[0]

			self.net = models.make_MLP(latent_dim, in_shape, hidden=[256], nonlin=nonlin, )

			self.deconv = nn.Sequential(*models.build_deconv_layers(settings, sizes=shapes[:-1],
			                                                        nonlin=nonlin, out_nonlin='sigmoid',
			                                                        norm_type=None))

		else:

			self.net = models.make_MLP(latent_dim, out_shape, hidden=[1200, 1200, 1200], nonlin=nonlin)

		self.uses_conv = net_type == 'conv'

		# self.set_optim(A)
		# self.set_scheduler(A)

	def forward(self, q):
		c = self.net(q)
		x = self.deconv(c) if self.uses_conv else c
		return x

	def decode(self, q):
		return self(q)

# endregion


# region Datasets


# endregion

# region Sup-Models

@fig.Component('sup-model')
class SupModel(fd.Visualizable, fd.Trainable_Model):
	def __init__(self, A):
		
		criterion = A.pull('criterion', 'cross-entropy')
		
		A.dout = criterion.din
		net = A.pull('net')

		super().__init__(net.din, criterion.dout)

		self.net = net
		self.criterion = util.get_loss_type(criterion)

		self.stats.new('confidence', 'error', *[f'error{i}' for i in range(len(self.criterion))])

		# self.set_optim(A)

	def forward(self, x):
		return self.net(x)

	def _visualize(self, info, logger):
		# if self._viz_counter % 5 == 0:
		# 	pass

		conf, pick = self.criterion.get_info(info.pred)

		confidence = conf.detach()
		correct = pick.sub(info.y).eq(0).float().detach()

		self.stats.update('confidence', confidence.mean())
		
		full_correct = 1
		for i, acc in enumerate(correct.t()):
			self.stats.update(f'error{i}', 1 - acc.mean())
			full_correct *= acc
		self.stats.update('error', 1 - full_correct.mean())

	def _step(self, batch, out=None):
		if out is None:
			out = util.TensorDict()

		# compute loss
		x, y = batch

		out.x, out.y = x, y

		pred = self(x)
		out.pred = pred

		loss = self.criterion(pred, y)
		out.loss = loss

		if self.train_me():
			self.optim.zero_grad()
			loss.backward()
			self.optim.step()

		return out

	def classify(self, x):
		return self.criterion.get_picks(self(x))

# endregion

# def get_name(A):
# 	if 'name' not in A:
# 		model, data = None, None
# 		arch = None
# 		if 'info' in A:
# 			if 'model_type' in A.info:
# 				model = A.info.model_type
# 			if 'dataset_type' in A.info:
# 				data = A.info.dataset_type
# 			if 'arch' in A.info:
# 				arch = A.info.arch
# 		if model is None:
# 			model = A.model._type
# 		if data is None:
# 			if 'name' in A.dataset:
# 				data = A.dataset.name
# 			else:
# 				data = A.dataset._type.split('-')[-1]
# 		name = '{}_{}'.format(model,data)
# 		if arch is not None:
# 			name = '{}_{}'.format(name, arch)
#
# 	if 'info' in A and 'extra' in A.info:
# 		name = '{}_{}'.format(name, A.info.extra)
#
# 	return name

if __name__ == '__main__':
	fig.entry('train')



