"""Freely wired neural network model with parallel operations.
"""

import code
import time
from copy import deepcopy

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.multiprocessing as mp
import numpy as np

from .utils import Timer
from .graph import Node, Edge, Graph
from .model_utils import activation_map, initialization_map

t = Timer()

class Op(nn.Module):
  def __init__(self, nodes, network, initialization):
    """Parallelized operation on network tape.

    # Arguments
      nodes: list of Node objects from graph module
      network: network object that this operation belongs to

    # Attributes
      input_indices: index tensor of shape (input_size, output_size), as
        inferred by node list.
      output_indices: 1D index tensor which points to locations in tape
        to write the outputs of this operation
      weights: torch.nn.Parameter of shape (input_size, output_size) with values
        initialized by He initialization
      bias: torch.nn.Parameter of shape (output_size) initialized at 0
    """
    super().__init__()
    self.nodes = nodes
    self.network = network
    # setup tape indices
    input_indices = []
    output_indices = []
    for node in nodes:
      input_indices.append(torch.tensor([inp.tape_index for inp in node.inputs],
                          dtype=torch.long))
      output_indices.append(node.tape_index)
      node.assigned = True
    self.input_indices = torch.nn.utils.rnn.pad_sequence(input_indices, batch_first=True).cuda()
    self.output_indices = torch.tensor(output_indices).cuda()
    unique, inverse = torch.unique(self.input_indices, return_inverse=True, sorted=False)
    self.unique_input_indices = unique
    self.inverse_input_indices = torch.unsqueeze(inverse, dim=0)
    # setup parameters
    weights = initialization_map[initialization](self.input_indices)
    weights = weights.cuda()
    weights[self.input_indices == 0] = 0
    self.weights = torch.nn.Parameter(weights)
    self.bias = torch.nn.Parameter(torch.zeros(self.input_indices.shape[0]).cuda())
    activation_index_map = dict()
    for i, node in enumerate(self.nodes):
      if node.activation not in activation_index_map.keys():
        activation_index_map.update({node.activation:[]})
      activation_index_map[node.activation].append(i)
    for key in activation_index_map.keys():
      activation_index_map[key] = torch.tensor(activation_index_map[key]).cuda()
    self.activation_index_map = activation_index_map

  def update_graph(self, op_i, op_num):
    """Called by Model.update_graph()
    """
    bias = self.bias.cpu()
    weights = self.weights.cpu()
    bias_grad, weights_grad = None, None
    if self.bias.grad is not None:
      bias_grad = self.bias.grad.cpu()
      weights_grad = self.weights.grad.cpu()
    for i, node in enumerate(self.nodes):
      node.bias = bias[i].item()
      if bias_grad is not None:
        node.grad = bias_grad[i].item()
      for j, in_edge in enumerate(node.in_edges):
        in_edge.weight = weights[i, j].item()
        if bias_grad is not None:
          in_edge.grad = weights_grad[i, j].item()
      end = "\n" if i + 1 == len(self.nodes) else "\r"
      print("op: [{}/{}], node: [{}/{}]".format(op_i + 1, op_num,
        i + 1, len(self.nodes)) + " " * 5, end=end)

  def forward(self, tape):
    # gather relevant values from network tape
    with torch.no_grad():
      batch_size = tape.shape[0]
      input_indices = self.inverse_input_indices
      input_indices = input_indices.expand(batch_size, -1, -1)
    _x = torch.index_select(tape, 1, self.unique_input_indices)
    _x = torch.unsqueeze(_x, dim=1).expand(-1, input_indices.shape[1], -1)
    x = torch.gather(_x, 2, input_indices)
    # weighted sum
    x = torch.mul(x, self.weights)
    x = torch.sum(x, dim=2)
    # bias
    x = torch.add(x, self.bias)
    # activation functions (operations can have different activations per neuron)
    for activation, indices in self.activation_index_map.items():
      indices = torch.unsqueeze(indices, 0).expand(batch_size, -1)
      activated = activation_map[activation](torch.gather(x, 1, indices))
      x = x.scatter(1, indices, activated)
    tape[:, self.output_indices] = x
    del x
    return tape

class Model(nn.Module):
  def __init__(self, graph, initialization="he"):
    """Freely wired neural network defined by graph data structure.

    # Arguments
      graph: graph datastructure
      initialization (optional): how to initialize weights. Uses He initialization by default.

    # Attributes
      tape_size: length of a flattened tensor representing all inputs,
        activations, and outputs of the NN. 0 index is always 0, input starts at
        index one. Equal to len(inputs + hidden + outputs) + 1
      tape: tensor of shape (batch, tape_size), used to store the activation
        state of the NN.
      ops: ordered list of Op objects, which perform operations on tape
        to produce an output.
    """
    super().__init__()
    self.inputs = graph.input_nodes
    self.hidden = graph.hidden_nodes
    self.outputs = graph.output_nodes
    self.nodes = graph.nodes
    self.tape_size = len(self.inputs + self.hidden + self.outputs) + 1
    self.input_size = len(self.inputs)
    self.output_size = len(self.outputs)
    self.tape = None
    self.ops = []
    self.initialization = initialization
    self.construct()
    # settings to compile
    self.optimizer = None
    self.loss_function = None
    # update randomized weights in graph
    self.update_graph()

  def construct(self):
    # initialize CUDA
    print("Initializing CUDA")
    _ = torch.zeros(1).cuda()
    i = 1 # indexing starts at 1 because the 0 index is always 0
    for input_node in self.inputs:
      input_node.tape_index = i
      input_node.assigned = True
      i += 1
    # time complexity for assigning indices to nodes is O(n^2)
    print("Indexing Nodes...")
    remaining_nodes = [node for node in self.hidden + self.outputs]
    num_nodes = len(remaining_nodes)
    assigned_nodes = 0
    ops = []
    # store node op ids in dict
    id_node_map = {}
    for node in remaining_nodes:
      if node.op_id not in id_node_map.keys():
        id_node_map.update({node.op_id:[]})
      id_node_map[node.op_id].append(node)
    # create ops
    while remaining_nodes != []:
      op_nodes = []
      for node in remaining_nodes:
        if all([inp.assigned for inp in node.inputs]):
          node.tape_index = i
          i += 1
          op_nodes.append(node)
          assigned_nodes += 1
          c = '\n' if assigned_nodes == num_nodes else '\r'
          print("[{}/{}]".format(assigned_nodes, num_nodes), end=c)
      # make sure all nodes of op id are available
      for node in op_nodes:
        fails = []
        if node.op_id != 'any' and node.op_id not in fails:
          for id_node in id_node_map[node.op_id]:
            if id_node not in op_nodes:
              fails.append(node.op_id)
              break
      if fails != []:
        for node in op_nodes:
          if node.op_id in fails:
            i -= 1
        op_nodes = [node for node in op_nodes if node.op_id not in fails]
      for node in op_nodes:
        remaining_nodes.remove(node)
      if op_nodes != []:
        ops.append(Op(op_nodes, self, self.initialization))
    self.ops = nn.ModuleList(ops)
    # determine the indices of the tape that correspond to outputs
    self.outputs = sorted(self.outputs, key=lambda x: x.output_index)
    output_indices = [node.tape_index for node in self.outputs]
    self.output_indices = torch.tensor(output_indices)

  def update_graph(self):
    """Update values in graph data structure used for visualization.
    """
    print("Updating graph")
    for i, op in enumerate(self.ops):
      op.update_graph(i, len(self.ops))
    return Graph(self.inputs, self.hidden, self.outputs)
  
  def count_params(self):
    return sum([len(node.in_edges) for node in self.hidden + self.outputs])

  def forward(self, x, output_grad=False):
    """Write input to tape and perform operations in order. Return
    output indices from tape.

    Args:
      x: input tensor of size (batch, input_size)
      output_grad: whether or not to calculate the gradient of the output.
        Defaults to false
    """
    if not isinstance(x, torch.Tensor):
      x = torch.tensor(x, dtype=torch.float).cuda()
    if len(x.shape) == 1:
      x = torch.unsqueeze(x, 0)
      batch_size = 1
    assert len(x.shape) == 2, "Input must be flattened batches (2D)"
    batch_size = x.shape[0]
    t.start('Initialize tape')
    if self.tape is not None and self.tape.shape[0] == batch_size:
      tape = self.tape.detach().fill_(0)
    else:
      tape = torch.zeros(batch_size, self.tape_size).cuda()
    t.end('Initialize tape')
    t.start('Writing to tape')
    tape[:, 1:self.input_size+1] = x
    t.end('Writing to tape')
    for i, op in enumerate(self.ops):
      t.start('Op {}'.format(i))
      tape = op.forward(tape)
      t.end('Op {}'.format(i))
    self.tape = tape
    output = tape[:, self.output_indices]
    if output_grad:
      output.backward()
      self.update_graph()
    return output

  def compile(self, optimizer, loss_function):
    if optimizer == 'adam':
      self.optimizer = torch.optim.Adam(self.parameters())
    elif optimizer == 'sgd':
      self.optimizer = torch.optim.SGD(self.parameters(), lr=0.01)
    if loss_function == 'mse':
      self.loss_function = torch.nn.MSELoss().cuda()
    elif loss_function == 'crossentropy':
      self.loss_function = torch.nn.CrossEntropyLoss().cuda()

  def fit(self, X_tr, y_tr, epochs=1, batch_size=32):
    # run checks
    if self.optimizer is None:
      raise RuntimeError("Compile model first before calling fit()")
    if not isinstance(X_tr, torch.Tensor):
      X_tr = torch.tensor(X_tr, dtype=torch.float).cuda()
    if not isinstance(y_tr, torch.Tensor):
      y_tr = torch.tensor(y_tr, dtype=torch.float).cuda()
    assert len(X_tr.shape) == 2, "X_tr must be flattened batches (2D)"
    assert X_tr.shape[1] == self.input_size, \
      "Input size given ({}) doesn't match this network's input size ({})".format(
        X_tr.shape[1], self.input_size)
    # begin training in batches
    print("Training on {} samples".format(len(X_tr)))
    num_batches = (len(X_tr) // batch_size)
    for epoch in range(0, epochs):
      epoch_loss = 0
      for batch in range(0, num_batches):
        start = batch * batch_size
        end = min((batch + 1) * batch_size, len(X_tr))
        range_select = torch.arange(start, end, dtype=torch.long).cuda()
        # optimization
        self.optimizer.zero_grad()
        out = self.forward(torch.index_select(X_tr, 0, range_select))
        loss = self.loss_function(out, torch.index_select(y_tr, 0, range_select))
        t.start('Backward')
        loss.backward(retain_graph=True)
        t.end('Backward')
        self.optimizer.step()
        # end optimization
        epoch_loss += loss.data
        bars = int(batch * 40 / num_batches)
        print("█" * bars, end='')
        print("░" * (40 - bars), end="\r")
      epoch_loss /= num_batches
      print("epoch [{}/{}] loss: {}".format(epoch + 1, epochs, epoch_loss), end=" " * 30 + "\n")
    self.tape = None
    self.update_graph()
