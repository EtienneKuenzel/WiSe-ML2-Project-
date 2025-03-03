import torch
import pickle
from tqdm import tqdm
from lop.algos.bp import Backprop, EWC_Policy, decreaseBackprop
from lop.nets.conv_net import ConvNet_PAU, ConvNet_TENT, ConvNet
from torch.nn.functional import softmax
from lop.nets.linear import MyLinear
from lop.utils.miscellaneous import nll_accuracy as accuracy
import numpy as np
import matplotlib.pyplot as plt
import torchvision.transforms as T
import torch.nn.functional as F
import torch.nn as nn
import time as time
import os
import torch.nn.init as init
import copy

# Function to display a batch of images
def show_batch(batch_x, batch_y, num_images_to_show=4, denormalize=False):
    """
    Displays a few images from the given batch with their corresponding labels.

    Parameters:
    - batch_x: Tensor of images (batch_size, channels, height, width)
    - batch_y: Tensor of labels (batch_size)
    - num_images_to_show: Number of images to display from the batch (default: 4)
    - denormalize: Whether to denormalize the image before displaying (default: False)
    """
    mean = [0.485, 0.456, 0.406]  # ImageNet mean
    std = [0.229, 0.224, 0.225]  # ImageNet std

    # Helper function to denormalize
    def denormalize_image(img):
        img = img.clone()
        for i in range(3):  # Assuming RGB images
            img[i] = img[i] * std[i] + mean[i]
        return img

    plt.figure(figsize=(12, 6))

    for i in range(min(num_images_to_show, len(batch_x))):
        img = batch_x[i]
        label = batch_y[i].item() if batch_y[i].dim() == 0 else batch_y[i].argmax().item()  # Handle one-hot labels

        if denormalize:
            img = denormalize_image(img)

        # Convert to PIL image for visualization
        img = T.ToPILImage()(img.cpu())

        plt.subplot(1, num_images_to_show, i + 1)
        plt.imshow(img)
        plt.title(f"Label: {label}")
        plt.axis('off')

    plt.show()


def get_activation(name):
    def hook(model, input, output):
        # Store activations
        if name not in activations:
            activations[name] = output.detach().clone()
        else:
            activations[name] = torch.cat((activations[name], output.detach().clone()), dim=0)

        # Store inputs to activations
        if name not in inputs_to_activations:
            inputs_to_activations[name] = input[0].detach().clone()  # input is a tuple
        else:
            inputs_to_activations[name] = torch.cat((inputs_to_activations[name], input[0].detach().clone()), dim=0)
    return hook

def average_activation_input(activations, layer):
    activation_inputs = []
    for layer_name, act in activations.items():
        if layer in layer_name:
            raw_input = act.detach()  # Detach to avoid tracking gradients (if needed)
            for neuron_input in raw_input.permute(1, 0):  # Iterate over neurons (assuming N x M tensor)
                activation_inputs.append(neuron_input.tolist())  # Append raw input values (as list)
    return activation_inputs


def load_imagenet(classes=[]):
    x_train, y_train, x_test, y_test = [], [], [], []
    for idx, _class in enumerate(classes):
        data_file = 'data/classes/' + str(_class) + '.npy'
        new_x = np.load(data_file)
        x_train.append(new_x[:600])
        x_test.append(new_x[600:])
        y_train.append(np.array([idx] * 600))
        y_test.append(np.array([idx] * 100))
    x_train = torch.tensor(np.concatenate(x_train))
    y_train = torch.from_numpy(np.concatenate(y_train))
    x_test = torch.tensor(np.concatenate(x_test))
    y_test = torch.from_numpy(np.concatenate(y_test))
    return x_train, y_train, x_test, y_test


def save_data(data, data_file):
    with open(data_file, 'wb+') as f:
        pickle.dump(data, f)


def custom_activation(x):
    return np.where(x > -3, np.maximum(0, x), -x - 3)

if __name__ == '__main__':
    num_tasks = 5
    use_gpu = 1
    mini_batch_size = 100
    run_idx = 3
    data_file = "outputc1.pkl"
    num_epochs =  1
    eval_every_tasks = 1
    save_folder = data_file + "model"
    # Device setup
    dev = torch.device("cuda:0") if use_gpu and torch.cuda.is_available() else torch.device("cpu")
    if use_gpu and torch.cuda.is_available():
        torch.set_default_tensor_type('torch.cuda.FloatTensor')
    if not os.path.exists(save_folder):
        os.makedirs(save_folder)
    # Initialize network
    net = ConvNet(activation="relu")
    #net =ConvNet_PAU()
    #net = ConvNet_TENT()
    #net = MyLinear(input_size=3072, num_outputs=classes_per_task)

    # Initialize learner
    learner = decreaseBackprop(
        net=net,
        step_size=0.01,
        opt="sgd",
        loss='nll',
        weight_decay=0,
        to_perturb=(0 != 0),
        perturb_scale=0,
        device=dev,
        momentum=0.9,
    )

    # Load class order
    with open('class_order', 'rb') as f:class_order = pickle.load(f)[run_idx]

    class_order = np.concatenate([class_order] * ((2 * num_tasks) // 1000 + 1))

    # Initialize accuracy tracking
    task_activations = torch.zeros(int(num_tasks/eval_every_tasks),3,3,128, 200)#numtasks, 3=layer, 3=CurrentTask+OOD(Next Task)+Adveserial Attack,100=Datapoints
    historical_accuracies = torch.zeros(num_tasks, 100)
    training_time = 0
    weight_layer = torch.zeros((num_tasks, 2, 128))
    bias_layer = torch.zeros(num_tasks, 2)
    saliency_maps = torch.zeros(num_tasks, 3, 128, 128)  # Example: num_tasks, 3 channels, 128x128 input size
    train_accuracies = torch.zeros((num_tasks, num_epochs), dtype=torch.float)
    test_accuracies = torch.zeros((num_tasks, num_epochs), dtype=torch.float)
    timetoperformance = torch.zeros(num_tasks)
    timetoperformanceregain = torch.ones(num_tasks,100) * num_epochs

    # Training loop

    for task_idx in range(num_tasks):
        start_time = time.time()
        print("Task : " + str(task_idx))
        x_train, y_train, x_test, y_test = load_imagenet(class_order[task_idx * 2:(task_idx + 1) * 2])
        x_train, x_test = x_train.float(), x_test.float()
        x_train, y_train = x_train.to(dev), y_train.to(dev)
        x_test, y_test = x_test.to(dev), y_test.to(dev)

        # Training loop
        for epoch_idx in range(num_epochs):
            example_order = np.random.permutation(1200)
            x_train, y_train = x_train[example_order], y_train[example_order]
            new_train_accuracies = torch.zeros((int(12),), dtype=torch.float)
            new_test_accuracies = torch.zeros((int(12),), dtype=torch.float)
            epoch_iter = 0
            for i, start_idx in enumerate(range(0, 1200, mini_batch_size)):
                batch_x = x_train[start_idx:start_idx + mini_batch_size]
                batch_y = y_train[start_idx:start_idx + mini_batch_size]
                loss, network_output = learner.learn(x=batch_x, target=batch_y, task=task_idx)
                with torch.no_grad():#train accuarcy
                    new_train_accuracies[epoch_iter] = accuracy(softmax(network_output, dim=1), batch_y).cpu()
                with torch.no_grad():#test accuarcy
                    test_batch_x = x_test[0:200]
                    test_batch_y = y_test[0:200]
                    new_test_accuracies[epoch_iter]  = accuracy(F.softmax(net.predict(x=test_batch_x)[0], dim=1), test_batch_y)
                    epoch_iter += 1
            test_accuracies[task_idx][epoch_idx] = new_test_accuracies.mean()
            train_accuracies[task_idx][epoch_idx] = new_train_accuracies.mean()
            if new_test_accuracies.mean() > 0.7:
                timetoperformance[task_idx] = epoch_idx
                print("--Learntime : " + str(epoch_idx))
                break


        # Update training time
        training_time += (time.time() - start_time)
        #Evaluation Current Task Activations
        activations = {}
        inputs_to_activations = {}
        hooks = []
        for name, layer in net.named_modules():
            if isinstance(layer, (torch.nn.Linear)): hooks.append(layer.register_forward_hook(get_activation(name)))
        x_train, y_train, x_test, y_test = load_imagenet(class_order[task_idx * 2:(task_idx + 1) * 2])
        x_train, x_test = x_train.float(), x_test.float()
        x_test, y_test = x_test.to(dev), y_test.to(dev)
        for i, start_idx in enumerate(range(0, x_test.shape[0], mini_batch_size)):
            test_batch_x = x_train[start_idx:start_idx + mini_batch_size]
            test_batch_y = y_train[start_idx:start_idx + mini_batch_size]
            network_output, _ = net.predict(x=test_batch_x)
        for layer in ["fc1", "fc2"]:
            task_activations[int(task_idx/eval_every_tasks)][0][int(layer[-1]) - 1] = torch.tensor(average_activation_input(activations, layer=layer), dtype=torch.float32)
        for hook in hooks: hook.remove()
        task_activations = task_activations.cpu()

        for t, previous_task_idx in enumerate(np.arange(max(0, task_idx - 9), task_idx + 1)):
            learnercopy = copy.deepcopy(learner)
            print("--Task : " + str(int(task_idx-previous_task_idx)))
            x_train, y_train, x_test, y_test = load_imagenet(class_order[previous_task_idx * 2:(previous_task_idx + 1) * 2])
            x_train, x_test = x_train.float(), x_test.float()
            x_train, y_train = x_train.to(dev), y_train.to(dev)
            x_test, y_test = x_test.to(dev), y_test.to(dev)
            for epoch_idx in range(num_epochs):
                example_order = np.random.permutation(1200)
                x_train, y_train = x_train[example_order], y_train[example_order]
                new_test_accuracies = torch.zeros((int(12),), dtype=torch.float)
                epoch_iter = 0
                for i, start_idx in enumerate(range(0, 1200, mini_batch_size)):
                    batch_x = x_train[start_idx:start_idx + mini_batch_size]
                    batch_y = y_train[start_idx:start_idx + mini_batch_size]
                    loss, network_output = learnercopy.learn(x=batch_x, target=batch_y, task=task_idx)
                    with torch.no_grad():  # test accuarcy
                        test_batch_x = x_test[0:200]
                        test_batch_y = y_test[0:200]
                        new_test_accuracies[epoch_iter] = accuracy(F.softmax(learnercopy.net.predict(x=test_batch_x)[0], dim=1),test_batch_y)
                        epoch_iter += 1
                if new_test_accuracies.mean() > 0.7:
                    timetoperformanceregain[task_idx][task_idx-previous_task_idx] = epoch_idx
                    print(epoch_idx)
                    break
    # Final save
    save_data({
        'last100_ttp' :timetoperformanceregain.cpu(),
        'ttp' :timetoperformance.cpu(),
        'train_accuracies': train_accuracies.cpu(),
        'test_accuracies': test_accuracies.cpu(),
        'time per task'  : training_time/num_tasks,
        'task_activations': task_activations.cpu(),
    }, data_file)

