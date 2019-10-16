import torch
from torch.nn.modules.utils import _pair
import numpy as np
import matplotlib.pyplot as plt
import plotly.graph_objs as go
from torchvision import transforms
from bindsnet.utils import reshape_locally_connected_weights
from time import time as t
import datetime
from tqdm import tqdm
from IPython import display


from bindsnet.datasets import MNIST
from bindsnet.encoding import PoissonEncoder
from bindsnet.network import Network, load
from bindsnet.learning import PostPre, WeightDependentPostPre
from bindsnet.network.monitors import Monitor, NetworkMonitor
from bindsnet.network.nodes import AdaptiveLIFNodes, Input
from bindsnet.network.topology import LocalConnection, Connection
from bindsnet.analysis.plotting import (
    plot_input,
    plot_conv2d_weights,
    plot_voltages,
    plot_spikes
    )

import streamlit as st


class LC_SNN:
    def __init__(self, norm=0.5, competitive_weight=-100., n_iter=1000, time_max=100, cropped_size=20,
                 kernel_size=12, n_filters = 25, stride=4,
                 load=False):
        self.n_iter = n_iter
        self.calibrated = False
        self.time_max = time_max
        self.cropped_size = cropped_size
        self.kernel_size = kernel_size
        self.n_filters = n_filters
        self.stride = stride
        if not load:
            self.create_network(norm=norm, competitive_weight=competitive_weight)
        else:
            pass

    def create_network(self, norm=0.5, competitive_weight=-100.):
        self.norm = norm
        self.competitive_weight = competitive_weight
        dt = 1
        intensity = 127.5

        self.train_dataset = MNIST(
            PoissonEncoder(time=self.time_max, dt=dt),
            None,
            "MNIST",
            download=False,
            train=True,
            transform=transforms.Compose([
                transforms.CenterCrop(self.cropped_size),
                 transforms.ToTensor(),
                 transforms.Lambda(lambda x: x * intensity)
                 ])
            )

        # Hyperparameters

        padding = 0
        conv_size = int((self.cropped_size - self.kernel_size + 2 * padding) / self.stride) + 1
        per_class = int((self.n_filters * conv_size * conv_size) / 10)
        tc_trace = 20.  # grid search check
        tc_decay = 20.
        thresh = -52
        refrac = 2

        self.wmin = 0
        self.wmax = 1

        # Network
        self.network = Network(learning=True)
        self.GlobalMonitor = NetworkMonitor(self.network, state_vars=('v', 's', 'w'))

        self.n_input = self.cropped_size**2
        self.input_layer = Input(n=self.n_input, shape=(1, self.cropped_size, self.cropped_size), traces=True, refrac=refrac)

        self.n_output = self.n_filters * conv_size * conv_size
        self.output_shape = int(np.sqrt(self.n_output))
        self.output_layer = AdaptiveLIFNodes(
            n=self.n_output,
            shape=(self.n_filters, conv_size, conv_size),
            traces=True,
            thres=thresh,
            trace_tc=tc_trace,
            tc_decay=tc_decay,
            theta_plus=0.05,
            tc_theta_decay=1e6)

        self.connection_XY = LocalConnection(
            self.input_layer,
            self.output_layer,
            n_filters=self.n_filters,
            kernel_size=self.kernel_size,
            stride=self.stride,
            update_rule=PostPre,
            norm=norm, #1/(kernel_size ** 2),#0.4 * self.kernel_size ** 2,  # norm constant - check
            nu=[1e-4, 1e-2],
            wmin=self.wmin,
            wmax=self.wmax)

        # competitive connections
        w = torch.zeros(self.n_filters, conv_size, conv_size, self.n_filters, conv_size, conv_size)
        for fltr1 in range(self.n_filters):
            for fltr2 in range(self.n_filters):
                if fltr1 != fltr2:
                    # change
                    for i in range(conv_size):
                        for j in range(conv_size):
                            w[fltr1, i, j, fltr2, i, j] = competitive_weight

        self.connection_YY = Connection(self.output_layer, self.output_layer, w=w)

        self.network.add_layer(self.input_layer, name='X')
        self.network.add_layer(self.output_layer, name='Y')

        self.network.add_connection(self.connection_XY, source='X', target='Y')
        self.network.add_connection(self.connection_YY, source='Y', target='Y')

        self.network.add_monitor(self.GlobalMonitor, name='Network')

        self.spikes = {}
        for layer in set(self.network.layers):
            self.spikes[layer] = Monitor(self.network.layers[layer], state_vars=["s"], time=self.time_max)
            self.network.add_monitor(self.spikes[layer], name="%s_spikes" % layer)

        self.voltages = {}
        for layer in set(self.network.layers) - {"X"}:
            self.voltages[layer] = Monitor(self.network.layers[layer], state_vars=["v"], time=self.time_max)
            self.network.add_monitor(self.voltages[layer], name="%s_voltages" % layer)

        weights_XY = self.network.connections[('X', 'Y')].w.reshape(self.cropped_size, self.cropped_size, -1).clone()
        weights_to_display = torch.zeros(0, self.cropped_size*int(self.n_output**0.5))
        i = 0
        while i < self.n_output:
            for j in range(int(self.n_output**0.5)):
                weights_to_display_row = torch.zeros(self.cropped_size, 0)
                for k in range(int(self.n_output**0.5)):
                    weights_to_display_row = torch.cat((weights_to_display_row, weights_XY[:, :, i]), dim=1)
                    i += 1
                weights_to_display = torch.cat((weights_to_display, weights_to_display_row), dim=0)
        self.weights_XY = weights_to_display.numpy()

        ################################################################################################################
        self.stride = self.stride
        self.conv_size = conv_size
        self.conv_prod = int(np.prod(conv_size))
        self.kernel_prod = int(np.prod(self.kernel_size))

        print(f'kernel_prod: {self.kernel_prod}')
        print(f'conv_prod: {self.conv_prod}')
        print(f'conv_size: {self.conv_size}')
        weights_to_display = reshape_locally_connected_weights(self.network.connections[('X', 'Y')].w,
                                                               n_filters=self.n_filters,
                                                               kernel_size=self.kernel_size,
                                                               conv_size=self.conv_size,
                                                               locations=self.network.connections[('X', 'Y')].locations,
                                                               input_sqrt=self.n_input)

        # print(self.network.connections[('X', 'Y')].locations)
        # print(self.network.connections[('X', 'Y')].locations.shape)
        #
        # mask = self.network.connections[('X', 'Y')].mask
        # print(len((mask==True).nonzero()))
        # source = self.network.connections[('X', 'Y')].w
        # #weights_to_plot = weights_to_display.masked_scatter(mask=mask, source=source)
        # #print(weights_to_plot)

        fig = go.Figure(data=go.Heatmap(z=weights_to_display.numpy(), colorscale='YlOrBr'))
        fig.update_layout(height=800, width=800)

    def train(self, n_iter=None, plot=False, vis_interval=10):
        n_iter = self.n_iter
        self.network.train(True)
        print('Training network...')
        train_dataloader = torch.utils.data.DataLoader(
            self.train_dataset, batch_size=1, shuffle=True)
        progress_bar = st.progress(0)
        status = st.empty()

        cnt = 0
        if plot:
            fig_weights = self.visualize()
            fig_weights.update_layout(width=800, height=800)
            st.write('Weights XY')
            weights_plot = st.plotly_chart(fig_weights)
            #fig_weights.show()

        t_start = t()
        for smth, batch in tqdm(list(zip(range(n_iter), train_dataloader))):
            progress_bar.progress(int((smth + 1)/n_iter*100))
            t_now = t()
            time_from_start = str(datetime.timedelta(seconds=(int(t_now - t_start))))
            speed = round((smth + 1) / (t_now - t_start), 2)
            time_left = str(datetime.timedelta(seconds=int((n_iter - smth) / speed)))
            status.text(f'{smth + 1}/{n_iter} [{time_from_start}] < [{time_left}], {speed}it/s')
            inpts = {"X": batch["encoded_image"].transpose(0, 1)}
            self.network.run(inpts=inpts, time=self.time_max, input_time_dim=1)

            if plot:
                if (t_now - t_start) / vis_interval > cnt:
                    fig_weights = self.visualize()
                    weights_plot.plotly_chart(fig_weights)

                    self._spikes = {
                        "X": self.spikes["X"].get("s").view(self.time_max, -1),
                        "Y": self.spikes["Y"].get("s").view(self.time_max, -1),
                        }
                    cnt += 1
                else:
                    pass
        self.network.reset_()  # Reset state variables
        self.network.train(False)

    def fit(self, X, y, n_iter=None):
        n_iter = self.n_iter
        self.network.train(True)
        print('Fitting network...')
        smth = 0

        for inp, label in tqdm(zip(X[:n_iter], y[:n_iter])):
            inpts = {"X": inp}
            self.network.run(inpts=inpts, time=self.time_max, input_time_dim=1)

            weights_XY = self.connection_XY.w
            weights_XY = weights_XY.reshape(self.cropped_size, self.cropped_size, -1)
            weights_to_display = torch.zeros(0, self.cropped_size*int(self.n_output**0.5))
            i = 0
            while i < self.n_output:
                for j in range(int(self.n_output**0.5)):
                    weights_to_display_row = torch.zeros(self.cropped_size, 0)
                    for k in range(int(self.n_output**0.5)):
                        weights_to_display_row = torch.cat((weights_to_display_row, weights_XY[:, :, i]), dim=1)
                        i += 1
                    weights_to_display = torch.cat((weights_to_display, weights_to_display_row), dim=0)

            self.weights_XY = weights_to_display.numpy()

            self._spikes = {
                "X": self.spikes["X"].get("s").view(self.time_max, -1),
                "Y": self.spikes["Y"].get("s").view(self.time_max, -1),
                }


        self.network.reset_()  # Reset state variables

        self.network.train(False);

    def class_from_spikes(self):
        sum_output = self.spikes['Y'].get('s').reshape(self.n_output, self.time_max).sum(1)
        res = torch.matmul(self.votes.type(torch.LongTensor), sum_output)
        return res.argmax()

    def debug_predictions(self, n_iter):
        train_dataloader = torch.utils.data.DataLoader(
            self.train_dataset, batch_size=1, shuffle=True)

        for i, batch in tqdm(list(zip(range(n_iter), train_dataloader))):
            inpts = {"X": batch["encoded_image"].transpose(0, 1)}
            self.network.run(inpts=inpts, time=self.time_max, input_time_dim=1)
            print(f'Network top class:{self.class_from_spikes()}\n Correct label: {batch["label"]}')

    def predict_many(self, n_iter=6000):
        y = []
        x = []
        self.network.train(False)
        train_dataloader = torch.utils.data.DataLoader(
            self.train_dataset, batch_size=1, shuffle=True)

        for i, batch in tqdm(list(zip(range(n_iter), train_dataloader))):
            inpts = {"X": batch["encoded_image"].transpose(0, 1)}

            self.network.run(inpts=inpts, time=self.time_max, input_time_dim=1)

            self._spikes = {
                "X": self.spikes["X"].get("s").view(self.time_max, -1),
                "Y": self.spikes["Y"].get("s").view(self.time_max, -1),
                }

            y.append(int(batch['label']))
            output = self._spikes['Y'].type(torch.int).sum(0)
            x.append(output)

        self.network.reset_()  # Reset state variables

        return tuple([x, y])

    def plot_weights_XY(self):
        weights_XY = self.network.connections[('X', 'Y')].w.reshape(self.cropped_size, self.cropped_size, -1)
        weights_to_display = torch.zeros(0, self.cropped_size*int(self.n_output**0.5))
        i = 0
        while i < self.n_output:
            for j in range(int(self.n_output**0.5)):
                weights_to_display_row = torch.zeros(self.cropped_size, 0)
                for k in range(int(self.n_output**0.5)):
                    weights_to_display_row = torch.cat((weights_to_display_row, weights_XY[:, :, i]), dim=1)
                    i += 1
                weights_to_display = torch.cat((weights_to_display, weights_to_display_row), dim=0)
        plt.figure(figsize=(15, 15))
        plt.imshow(weights_to_display, cmap='YlOrBr')
        plt.colorbar()

    def load(self, file_path):
        self.network = load(file_path)
        self.n_iter = 60000

        dt = 1
        intensity = 127.5

        self.train_dataset = MNIST(
            PoissonEncoder(time=self.time_max, dt=dt),
            None,
            "MNIST",
            download=False,
            train=True,
            transform=transforms.Compose(
                [transforms.ToTensor(), transforms.Lambda(lambda x: x * intensity)]
                )
            )

        self.spikes = {}
        for layer in set(self.network.layers):
            self.spikes[layer] = Monitor(self.network.layers[layer], state_vars=["s"], time=self.time_max)
            self.network.add_monitor(self.spikes[layer], name="%s_spikes" % layer)

        self.voltages = {}
        for layer in set(self.network.layers) - {"X"}:
            self.voltages[layer] = Monitor(self.network.layers[layer], state_vars=["v"], time=self.time_max)
            self.network.add_monitor(self.voltages[layer], name="%s_voltages" % layer)

        weights_XY = self.network.connections[('X', 'Y')].w

        weights_XY = weights_XY.reshape(self.cropped_size, self.cropped_size, -1)
        weights_to_display = torch.zeros(0, self.cropped_size*int(self.n_output**0.5))
        i = 0
        while i < self.n_output:
            for j in range(int(self.n_output**0.5)):
                weights_to_display_row = torch.zeros(self.cropped_size, 0)
                for k in range(int(self.n_output**0.5)):
                    weights_to_display_row = torch.cat((weights_to_display_row, weights_XY[:, :, i]), dim=1)
                    i += 1
                weights_to_display = torch.cat((weights_to_display, weights_to_display_row), dim=0)

        self.weights_XY = weights_to_display.numpy()

        # TODO formatted weights to display

    def show_neuron(self, n):
        weights_to_show = self.network.connections[('X', 'Y')].w.reshape(self.cropped_size, self.cropped_size, -1).clone()
        weights_to_show[:, :, n-1] = torch.ones(self.cropped_size, self.cropped_size)
        weights_to_display = torch.zeros(0, self.cropped_size*int(self.n_output**0.5))
        i = 0
        while i < self.n_output:
            for j in range(int(self.n_output**0.5)):
                weights_to_display_row = torch.zeros(self.cropped_size, 0)
                for k in range(int(self.n_output**0.5)):
                    weights_to_display_row = torch.cat((weights_to_display_row, weights_to_show[:, :, i]), dim=1)
                    i += 1
                weights_to_display = torch.cat((weights_to_display, weights_to_display_row), dim=0)


        plt.figure(figsize=(15, 15))
        plt.title('Weights XY')

        plt.imshow(weights_to_display.numpy(), cmap='YlOrBr')
        plt.colorbar()

    def calibrate_top_classes(self, n_iter=100):
        print('Calibrating top classes for each neuron...')
        (x, y) = self.predict_many(n_iter=n_iter)
        votes = torch.zeros(11, self.n_output)
        votes[10, :] = votes[10, :].fill_(1/(2*n_iter))
        for (label, layer) in zip(y, x):
            for i, spike_sum in enumerate(layer):
                votes[label, i] += spike_sum
        for i in range(10):
            votes[i, :] = votes[i, :] / len((np.array(y) == i).nonzero()[0])
        top_classes = votes.argmax(dim=0).numpy()
        # top_classes_formatted = np.where(top_classes!=10, top_classes, None)
        self.top_classes = top_classes
        self.votes = votes
        self.calibrated = True
        return top_classes, votes

    def accuracy(self, n_iter):
        self.network.train(False)
        if not self.calibrated:
            self.calibrate_top_classes(n_iter=self.n_iter)

        train_dataloader = torch.utils.data.DataLoader(
            self.train_dataset, batch_size=1, shuffle=True)

        print('Calculating accuracy...')

        x = []
        y = []

        for i, batch in tqdm(list(zip(range(n_iter), train_dataloader))):
            inpts = {"X": batch["encoded_image"].transpose(0, 1)}

            self.network.run(inpts=inpts, time=self.time_max, input_time_dim=1)

            self._spikes = {
                "X": self.spikes["X"].get("s").view(self.time_max, -1),
                "Y": self.spikes["Y"].get("s").view(self.time_max, -1),
                }

            output = self._spikes['Y'].type(torch.int).sum(0)
            top3 = output.argsort()[0:3]
            label = int(batch['label'])
            n_1, n_2, n_3 = top3[0], top3[1], top3[2]
            n_best = n_1
            if output[n_1] * self.votes[label][n_1] > output[n_2] * self.votes[label][n_2]:
                if output[n_2] * self.votes[label][n_2] > output[n_3] * self.votes[label][n_3]:
                    pass
                else:
                    if output[n_1] * self.votes[label][n_1] > output[n_3] * self.votes[label][n_3]:
                        pass
            else:
                if output[n_2] * self.votes[label][n_2] > output[n_3] * self.votes[label][n_3]:
                    n_best = n_2
                else:
                    if output[n_3] * self.votes[label][n_3] > output[n_1] * self.votes[label][n_1]:
                        n_best = n_3

            x.append(self.top_classes[n_best])
            y.append(label)

        corrects = []
        for i in range(len(x)):
            if x[i] == y[i]:
                corrects.append(1)
            else:
                corrects.append(0)
        corrects = np.array(corrects)

        self.network.reset_()
        print(f'Accuracy: {corrects.mean()}')
        return corrects.mean()

    def score(self, X, y_correct):
        self.network.train(False)
        if not self.calibrated:
            self.calibrate_top_classes()
        print('Calculating score...')

        x = []

        for inp, label in zip(X, y_correct):
            inpts = {'X': inp}

            self.network.run(inpts=inpts, time=self.time_max, input_time_dim=1)

            self._spikes = {
                "X": self.spikes["X"].get("s").view(self.time_max, -1),
                "Y": self.spikes["Y"].get("s").view(self.time_max, -1),
                }

            output = self._spikes['Y'].type(torch.int).sum(0)
            top3 = output.argsort()[0:3]
            n_1, n_2, n_3 = top3[0], top3[1], top3[2]
            x.append(self.top_classes[n_1])

        corrects = []
        for i in range(len(x)):
            if x[i] == y_correct[i]:
                corrects.append(1)
            else:
                corrects.append(0)
        corrects = np.array(corrects)

        self.network.reset_()
        print(f'Accuracy: {corrects.mean()}')
        return corrects.mean()

    def get_params(self, **args):
        return {'norm': self.norm,
                'competitive_weight': self.competitive_weight,
                'n_iter': self.n_iter
                }

    def set_params(self, norm, competitive_weight, n_iter):
        display.clear_output(wait=True)
        return LC_SNN(norm=norm, competitive_weight=competitive_weight, n_iter=n_iter)

    def visualize(self):
        # weights_XY = self.network.connections[('X', 'Y')].w.reshape(self.cropped_size, self.cropped_size, -1).clone()
        # weights_to_display = torch.zeros(0, self.cropped_size*int(self.n_output**0.5))
        # i = 0
        # while i < self.n_output:
        #     for j in range(int(self.n_output**0.5)):
        #         weights_to_display_row = torch.zeros(self.cropped_size, 0)
        #         for k in range(int(self.n_output**0.5)):
        #             weights_to_display_row = torch.cat((weights_to_display_row, weights_XY[:, :, i]), dim=1)
        #             i += 1
        #         weights_to_display = torch.cat((weights_to_display, weights_to_display_row), dim=0)

        weights_to_display = reshape_locally_connected_weights(self.network.connections[('X', 'Y')].w,
                                                               n_filters=self.n_filters,
                                                               kernel_size=self.kernel_size,
                                                               conv_size=self.conv_size,
                                                               locations=self.network.connections[('X', 'Y')].locations,
                                                               input_sqrt=self.n_input)

        fig_weights = go.Figure(data=go.Heatmap(z=weights_to_display.numpy(), colorscale='YlOrBr'))
        fig_weights.update_layout(width=800, height=800)
        #st.plotly_chart(fig_weights)
        #fig_weights.show()
        #print(weights_XY[:, :, 0])
        return fig_weights

    def __repr__(self):
        # return f'LC_SNN network with parameters:\nnorm = {self.norm}\ncompetitive_weights={self.competitive_weight}' \
        #        f'\nn_iter={self.n_iter}'
        return f'LC_SNN network with parameters:\n {self.get_params()}'