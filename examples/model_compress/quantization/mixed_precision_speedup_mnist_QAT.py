import torch
import torch.nn.functional as F
from torchvision import datasets, transforms
import numpy as np

from nni.algorithms.compression.pytorch.quantization import QAT_Quantizer
from nni.compression.pytorch.quantization_speedup import ModelSpeedupTensorRT

class Mnist(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = torch.nn.Conv2d(1, 20, 5, 1)
        self.conv2 = torch.nn.Conv2d(20, 50, 5, 1)
        self.fc1 = torch.nn.Linear(4 * 4 * 50, 500)
        self.fc2 = torch.nn.Linear(500, 10)
        self.relu1 = torch.nn.ReLU6()
        self.relu2 = torch.nn.ReLU6()
        self.relu3 = torch.nn.ReLU6()
        self.maxpool1 = torch.nn.MaxPool2d(2, 2)
        self.maxpool2 = torch.nn.MaxPool2d(2, 2)

    def forward(self, x):
        x = self.relu1(self.conv1(x))
        x = self.maxpool1(x)
        x = self.relu2(self.conv2(x))
        x = self.maxpool2(x)
        x = x.view(-1, 4 * 4 * 50)
        x = self.relu3(self.fc1(x))
        x = self.fc2(x)
        return F.log_softmax(x, dim=1)


def train(model, device, train_loader, optimizer):
    model.train()
    for batch_idx, (data, target) in enumerate(train_loader):
        data, target = data.to(device), target.to(device)
        optimizer.zero_grad()
        output = model(data)
        loss = F.nll_loss(output, target)
        loss.backward()
        optimizer.step()
        if batch_idx % 100 == 0:
            print('{:2.0f}%  Loss {}'.format(100 * batch_idx / len(train_loader), loss.item()))

def test(model, device, test_loader):
    model.eval()
    test_loss = 0
    correct = 0
    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            output = model(data)
            test_loss += F.nll_loss(output, target, reduction='sum').item()
            pred = output.argmax(dim=1, keepdim=True)
            correct += pred.eq(target.view_as(pred)).sum().item()
    test_loss /= len(test_loader.dataset)

    print('Loss: {}  Accuracy: {}%)\n'.format(
        test_loss, 100 * correct / len(test_loader.dataset)))

def get_testset(trans, test_loader):
    set = next(iter(test_loader))
    test_set, test_labels = set[0].numpy(), set[1].numpy()
    return test_set, test_labels

def check_accuracy(preds, labels):
    start_idx = 0
    num_correct_all = 0
    for pred in preds:
        pred = np.argmax(pred.reshape(-1, 10), axis=1)
        effective_shape = pred.shape[0]
        num_correct = np.count_nonzero(np.equal(pred, labels[start_idx:start_idx+effective_shape]))
        num_correct_all = num_correct_all + num_correct
        start_idx = start_idx + effective_shape
    print("accuracy: ", 100 * num_correct_all / 1000)

def main():
    torch.manual_seed(0)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    trans = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
    train_loader = torch.utils.data.DataLoader(
        datasets.MNIST('data', train=True, download=True, transform=trans),
        batch_size=64, shuffle=True)
    test_loader = torch.utils.data.DataLoader(
        datasets.MNIST('data', train=False, transform=trans),
        batch_size=1000, shuffle=True)

    model = Mnist()

    configure_list = [{
            'quant_types': ['weight', 'output'],
            'quant_bits': {'weight':8, 'output':8},
            'op_names': ['conv1']
        }, {
            'quant_types': ['output'],
            'quant_bits': {'output':8},
            'op_names': ['relu1']
        }, {
            'quant_types': ['weight', 'output'],
            'quant_bits': {'weight':8, 'output':8},
            'op_names': ['conv2']
        }, {
            'quant_types': ['output'],
            'quant_bits': {'output':8},
            'op_names': ['relu2']
        }, {
            'quant_types': ['weight', 'output'],
            'quant_bits': {'weight':8, 'output':8},
            'op_names': ['fc1']
        }, {
            'quant_types': ['output'],
            'quant_bits': {'output':8},
            'op_names': ['relu3']
        }, {
            'quant_types': ['weight', 'output'],
            'quant_bits': {'weight':8, 'output':8},
            'op_names': ['fc2']
        }
    ]

    optimizer = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.5)
    quantizer = QAT_Quantizer(model, configure_list, optimizer)
    quantizer.compress()

    model.to(device)
    for epoch in range(1):
        print('# Epoch {} #'.format(epoch))
        train(model, device, train_loader, optimizer)
        test(model, device, test_loader)

    model_path = "mnist_model.pth"
    calibration_path = "mnist_calibration.pth"
    calibration_config = quantizer.export_model(model_path, calibration_path)

    test(model, device, test_loader)

    print("calibration_config: ", calibration_config)

    batch_size = 32
    input_shape = (batch_size, 1, 28, 28)
    test_set, test_labels = get_testset(trans, test_loader)

    engine = ModelSpeedupTensorRT(model, input_shape, config=calibration_config, batchsize=batch_size)
    engine.compress()
    output, time = engine.inference(test_set)

    check_accuracy(output, test_labels)
    print("elapsed_time: ", time)

if __name__ == '__main__':
    main()