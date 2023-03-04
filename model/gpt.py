import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset

from model.components.decoder import Decoder
from model.utils.mask import generate_mask
from model.metric import BLEU
from model.loss import Perplexity
from typing import Union, Callable
from model.components.classifier import Classifier
from torchsummary import summary

import os

device = torch.device('cuda' if torch.cuda.is_available() else "cpu")

class GPTModel(nn.Module):
    def __init__(self, token_size: int,  
                n: int, 
                embedding_dim: int, 
                heads: int, 
                d_ff: int, 
                dropout_rate: float, 
                eps: float,
                activation: Union[str, Callable[[torch.Tensor], torch.Tensor]]) -> None:
        super().__init__()
        self.embedding_layer = nn.Embedding(num_embeddings=token_size, embedding_dim=embedding_dim)
        self.decoder = Decoder(token_size=token_size, n=n, embedding_dim=embedding_dim, heads=heads, d_ff=d_ff, dropout_rate=dropout_rate, eps=eps, activation=activation)
        self.to(device)

    def forward(self, x: torch.Tensor, mask: torch.Tensor, training: bool) -> torch.Tensor:
        x = self.embedding_layer(x)
        output = self.decoder(x, mask, training)
        return output



class GPTPretrain:
    def __init__(self,
                token_size: int, 
                n: int = 12, 
                embedding_dim: int = 768, 
                heads: int = 12, 
                d_ff: int = 2048, 
                dropout_rate: float = 0.1, 
                eps: float = 0.1, 
                activation: Union[str, Callable[[torch.Tensor], torch.Tensor]] = F.relu,
                learning_rate: float = 0.0006,
                optimizer: optim.Optimizer = optim.Adam,
                checkpoint: str = None):
        self.model = GPTModel(token_size=token_size, n=n, embedding_dim=embedding_dim, heads=heads, d_ff=d_ff, dropout_rate=dropout_rate, eps=eps, activation=activation)

        self.embedding_dim = embedding_dim
        self.checkpoint = checkpoint
        self.optimizer = optimizer(params = self.model.parameters(), lr=learning_rate)
        # self.metric = BLEU()
        self.perplexity_loss = Perplexity()
        self.criterion = nn.CrossEntropyLoss()

        self.entropy_loss = 0.0
        # self.bleu_score = 0.0
        self.perplexity = 0.0

        self.epoch = 0

        if self.checkpoint is not None:
            self.load_model(checkpoint)

    def sumary(self):
        summary(self.model)

    def build_pretrain_dataset(self, data: torch.Tensor, batch_size: int, shuffle: bool = True):
        dataset = TensorDataset(data)
        dataloader = DataLoader(dataset=dataset, batch_size=batch_size, shuffle=shuffle)
        return dataloader

    def __save_model(self, checkpoint: str):
        torch.save({
            'model_state_dict': self.model.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'epoch': self.epoch
        }, checkpoint)

        print(f"Model Saved at {checkpoint}")

    def __load_model(self, checkpoint: str):
        if os.path.exists(checkpoint):
            checkpoint_data = torch.load(checkpoint)
            self.model.load_state_dict(checkpoint_data['model_state_dict'])
            self.optimizer.load_state_dict(checkpoint_data['optimizer_state_dict'])
            self.epoch = checkpoint_data['epoch']

    def save_model(self, path: str = None):
        if path is not None:
            self.__save_model(path)
            self.checkpoint = path
        elif self.checkpoint is not None:
            self.__save_model(self.checkpoint)
        else:
            print("Checkpoint not found")

    def load_model(self, path: str = None):
        if path is not None:
            self.__load_model(path)
            self.checkpoint = path
        elif self.checkpoint is not None:
            self.__load_model(self.checkpoint)
        else:
            print("Checkpoint not found")

        
    def cross_entropy_loss(self, outputs: torch.Tensor, labels: torch.Tensor) -> torch.Tensor:
        batch_size = labels.size(0)
        loss = 0.0
        for batch in range(batch_size):
            loss += self.criterion(outputs[batch], labels[batch])

        loss = loss/batch_size

        return loss

    def pretrain_step(self, data: torch.Tensor):
        self.optimizer.zero_grad()
        inputs = data[:, :-1]
        labels = data[:, 1:]

        _, look_ahead_mask = generate_mask(inputs)

        outputs = self.model(inputs, look_ahead_mask, True)

        # _, preds = torch.max(outputs, dim=-1)

        
        loss = self.cross_entropy_loss(outputs=outputs, labels=labels)
        loss.backward()
        self.optimizer.step()

        self.entropy_loss += loss.item()

        # self.bleu_score += self.metric.score(outputs=preds, labels=labels)
        self.perplexity += self.perplexity_loss.loss(entropy_loss=loss)

    def fit(self, data: torch.Tensor,batch_size: int = 1, epochs: int = 1, shuffle: bool = True, mini_batch: int = 1):
        if self.checkpoint is not None:
            self.load_model(self.checkpoint)
        self.model.train()
        self.sumary()
        dataloader = self.build_pretrain_dataset(data=data, batch_size=batch_size, shuffle=shuffle)
        epoch_loss = 0.0
        batches = len(dataloader)
        for _ in range(epochs):
            for index, batch in enumerate(dataloader, 0):
                self.pretrain_step(data=batch[0].to(device))
                epoch_loss += self.entropy_loss
                if index%mini_batch == mini_batch-1 or index == batches-1:
                    print(f"Epoch: {self.epoch + 1} Batch: {index+1} Loss: {(self.entropy_loss/mini_batch):.4f} Perplexity Loss: {(self.perplexity/mini_batch):.4f}")

                    # Set default
                    self.entropy_loss = 0.0
                    # self.bleu_score = 0.0
                    self.perplexity = 0.0
            self.epoch += 1
            mean_loss = epoch_loss/(index+1)
            
            """ if mean_loss <= stop_loss:
                print("Early Stopping")
                break
            epoch_loss = 0.0 """

        if self.checkpoint is not None:
            self.__save_model(self.checkpoint)

class GPTFineTune(nn.Module):
    def __init__(self, 
                token_size: int,
                n: int = 12, 
                embedding_dim: int = 768, 
                heads: int = 12, 
                d_ff: int = 2048, 
                dropout_rate: float = 0.1, 
                eps: float = 0.1, 
                activation: Union[str, Callable[[torch.Tensor], torch.Tensor]] = F.relu) -> None:
        super().__init__()
        
        self.pretrained_model = GPTModel(
            token_size=token_size,
            n=n,
            embedding_dim=embedding_dim,
            heads=heads,
            d_ff=d_ff,
            dropout_rate=dropout_rate,
            eps=eps,
            activation=activation
        )
        
        self.classifier = Classifier()


        self.to(device)

    def forward(self, x: torch.Tensor, mask: torch.Tensor, training: bool):
        x = self.pretrained_model(x, mask, training)
        x = self.classifier(x)
        return x     

class GPT:
    def __init__(self,
                token_size: int,
                n: int = 12, 
                embedding_dim: int = 768, 
                heads: int = 12, 
                d_ff: int = 2048, 
                dropout_rate: float = 0.1, 
                eps: float = 0.1, 
                activation: Union[str, Callable[[torch.Tensor], torch.Tensor]] = F.relu,
                optimizer: optim.Optimizer = optim.Adam,
                learning_rate: float = 0.0006,
                checkpoint: str = None) -> None:
        self.model = GPTFineTune(
            token_size=token_size,
            n=n,
            embedding_dim=embedding_dim,
            heads=heads,
            d_ff=d_ff,
            dropout_rate=dropout_rate,
            eps=eps,
            activation=activation
        )
        
        self.optimizer = optim.Adam(params=self.model.parameters(), lr=learning_rate)
        self.criterion = nn.CrossEntropyLoss()
        self.epoch = 0

        self.checkpoint = checkpoint

        self.entropy_loss = 0.0

        self.training = False

        if self.checkpoint is not None:
            self.load_model(checkpoint)

    def build_dataset(self, inputs: torch.Tensor, labels: torch.Tensor, batch_size: int, shuffle: bool):
        dataset = TensorDataset(inputs, labels)
        dataloader = DataLoader(dataset=dataset, batch_size=batch_size, shuffle=shuffle)

        return dataloader

    def loss_function(self, outputs: torch.Tensor, labels: torch.Tensor) -> torch.Tensor:
        batch_size = labels.size(0)
        loss = 0.0
        for batch in range(batch_size):
            loss += self.criterion(outputs[batch], labels[batch])

        loss = loss/batch_size

        return loss


    def train_step(self, inputs: torch.Tensor, labels: torch.Tensor):
        self.optimizer.zero_grad()
        
        _, look_ahead_mask = generate_mask(inputs)
        outputs = self.model(inputs, look_ahead_mask, True)
        loss = self.loss_function(outputs, labels)

        loss.backward()
        self.optimizer.step()

        self.entropy_loss += loss.item()
        
    def __load_model(self, path: str) -> None:
        if os.path.exists(path):
            checkpoint = torch.load(path)
            self.model.load_state_dict(checkpoint['model_state_dict'])
            self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
            self.epoch = checkpoint['epoch']

            for params in self.model.pretrained_model.parameters():
                params.requires_grad = False
            self.model.pretrained_model.decoder.linear.requires_grad_(True)
            self.training = True


    def load_model(self, path: str = None):
        if path is None and self.checkpoint is not None:
            self.__load_model(self.checkpoint)
        elif path is not None:
            self.__load_model(path)
            self.checkpoint = path

    def __save_model(self, path: str) -> None:
        torch.save({
            'model_state_dict': self.model.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'epoch': self.epoch
        }, path)
        print(f"Model is saved at {path}")
        

    def save_model(self, path: str = None):
        if path is None and self.checkpoint is not None:
            self.__save_model(self.checkpoint)
        elif path is not None:
            self.__save_model(path)
            self.checkpoint = path    

    def load_pretrained_model(self, path: str) -> None:
        checkpoint = torch.load(path)
        self.model.pretrained_model.load_state_dict(checkpoint['model_state_dict'])
        for params in self.model.pretrained_model.parameters():
            params.requires_grad = False
        self.model.pretrained_model.decoder.linear.requires_grad_(True)
        
        print("Loaded Pretrained Model")


    def fit(self, inputs: torch.Tensor, labels: torch.Tensor,  pretrained_path: str = None, batch_size: int = 1, epochs: int = 1, mini_batch: int = 1, shuffle_data: bool = True):
        
        if self.checkpoint is not None:
            self.load_model(self.checkpoint)
        if self.training == False:
            self.load_pretrained_model(pretrained_path)
        self.model.train()
        self.sumary()
        dataloader = self.build_dataset(inputs=inputs, labels=labels, batch_size=batch_size, shuffle=shuffle_data)
        for _ in range(epochs):
            total_batch = len(dataloader)
            for index, data in enumerate(dataloader, 0):
                inputs = data[0].to(device)
                labels = data[1].to(device)

                self.train_step(inputs=inputs, labels=labels)


                if index%mini_batch == mini_batch-1 or index == total_batch-1:
                    print(f"Epoch: {self.epoch+1} Batch: {index+1} Loss: {(self.entropy_loss/mini_batch):.4f}")
                    self.entropy_loss = 0.0
            self.epoch +=1 

        if self.checkpoint is not None:
            self.__save_model(self.checkpoint)

    def __predict_token(self, input: torch.Tensor) -> torch.Tensor:
        _, look_ahead_mask = generate_mask(input)
        
        outputs = self.model(input, look_ahead_mask, False)

        predict = outputs[:, -1, :]
        _, token_id = torch.max(predict, dim=-1)

        return token_id
    def sumary(self):
        if self.checkpoint is not None:
            self.load_model(self.checkpoint)
        summary(self.model)

    def predict(self, data: torch.Tensor, limit_tokens: int, end_token: int):
        """ if self.checkpoint is not None:
            self.load_model(self.checkpoint)
        else:
            print("Model is not Trained")
            return None """
        self.model.eval()
        data = data.to(device)

        for _ in range(limit_tokens):
            token = self.__predict_token(input=data)
            if token == end_token:
                break
            
            data = torch.concat([data, token.unsqueeze(0)], dim=-1)

        return data