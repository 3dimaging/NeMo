# -*- coding: utf-8 -*-

# =============================================================================
# Copyright (c) 2020 NVIDIA. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================

from collections import namedtuple
from typing import Dict, Optional

from nemo.core import OperationMode
from nemo.core.neural_interface import NeuralInterface
from nemo.core.neural_types import (
    NeuralPortNameMismatchError,
    NeuralPortNmTensorMismatchError,
    NeuralType,
    NeuralTypeComparisonResult,
)
from nemo.utils.bound_outputs import BoundOutputs


class NeuralGraph(NeuralInterface):
    """
        Neural Graph class stores dynamically defined graphs of connected Neural Modules.
    """

    def __init__(self, operation_mode=OperationMode.both, name=None):
        """
            Constructor. Initializes graph variables.

            Args:
                operation_mode: Graph operation mode, that will be propagated along modules during graph creation.
                [training | eval | both] (DEFAULT: both)
                name: Name of the graph (optional)
        """
        # Initialize the inferface.
        super().__init__(name)

        # Register graph.
        self._name = self._app_state.register_graph(self, name)

        # Store name and operation mode.
        self._operation_mode = operation_mode

        # Input ports and tensors - empty for now.
        self._bound_input_ports = {}
        self._bound_input_tensors = {}
        # List of modules of bound inputs - so we will update their output tensors when the "bound"
        # input port will be connected.
        self._bound_input_modules = {}

        # Bound outputs.
        self._bound_outputs = BoundOutputs()

        # "Modules" - list of modules constituting edges in a given graph.
        self._modules = {}
        # "Steps": ordered execution of modules in a graph.
        self._steps = []

    def __call__(self, **kwargs):
        """
            This method "nests" one existing neural graph into another one.
            Also checks if all inputs were provided and properly connects them.

        """
        # Test operation modes of the nested graphs.
        outer_mode = self._app_state.active_graph.operation_mode
        inner_mode = self.operation_mode

        if inner_mode == OperationMode.inference and outer_mode == OperationMode.training:
            raise TypeError("Cannot nest 'inference' graph into 'training'")

        if inner_mode == OperationMode.training and outer_mode == OperationMode.inference:
            raise TypeError("Cannot nest 'training' graph into 'inference'")

        if inner_mode == OperationMode.training and outer_mode == OperationMode.both:
            raise TypeError("Cannot nest 'training' graph into 'both'")

        if inner_mode == OperationMode.inference and outer_mode == OperationMode.both:
            raise TypeError("Cannot nest 'inference' graph into 'both'")

        # print(" Neural Graph {} __call__".format(self._name))
        # Get input and output ports definitions.
        input_port_defs = self.input_ports

        # TODO: check graph operation mode compatibility.

        # "Copy" all the operations from the previous graph.
        for step in self._steps:
            self._app_state.active_graph.record_step(*step)

        # print(self._steps)

        # Iterate through all passed parameters - input ports.
        # Port content: NmTensor or NeuralGraph (binding).
        for input_port_name, input_object in kwargs.items():
            # make sure that passed arguments correspond to input port names
            if input_port_name not in input_port_defs.keys():
                raise NeuralPortNameMismatchError("Wrong input port name: {0}".format(input_port_name))

            # Check what was actually passed.
            if isinstance(input_object, NeuralGraph):

                # TODO: make sure that input_object ==  self._app_state.active_graph ?!?!

                # Bind this input port to a neural graph.
                input_object.bind_input(input_port_name, input_port_defs[input_port_name], self)

                # It is "compatible by definition";), so we don't have to check this port further.

            else:  # : input_object is a Tensor!
                # Compare input port definition with the received definition.
                type_comatibility = input_port_defs[input_port_name].compare(input_object)
                if (
                    type_comatibility != NeuralTypeComparisonResult.SAME
                    and type_comatibility != NeuralTypeComparisonResult.GREATER
                ):
                    raise NeuralPortNmTensorMismatchError(
                        "\n\nIn {0}. \n"
                        "Port: {1} and a NmTensor it was fed are \n"
                        "of incompatible neural types:\n\n{2} \n\n and \n\n{3}"
                        "\n\nType comparison result: {4}".format(
                            self.__class__.__name__,
                            input_port_name,
                            input_port_defs[input_port_name],
                            input_object,
                            type_comatibility,
                        )
                    )

                # Reaching that point means that we accepted input to a bound port.
                # Need to connect it - add bound module as consumer.
                consumer = self._bound_input_modules[input_port_name]
                port_name = input_port_name  # For now!
                input_object.add_consumer(consumer, port_name)

                # The current graph parsing requires us to update all outputs of
                # a module that "accepted" the input.
                # Update means changing the original producer_args for ALL IN THE GRAPH!! # TODO!
                producer = self._bound_input_modules[input_port_name]
                for output_tensor in self._bound_outputs.values():
                    if output_tensor.producer.name == producer.name:
                        # Set "input port value" to new content - which indicates tensor (and producer)
                        # that will be used during graph backward traverse.
                        output_tensor.producer_args[port_name] = input_object  # i.e. Tensor.

        # Create the module outputs.
        # This part is different from Neural Module.
        # Now the goal is NOT to create NEW "tensors", but to return the BOUND ones!
        if len(self._bound_outputs) == 1:
            # Return the single tensor.
            results = next(iter(self._bound_outputs.values()))
        else:
            # Create a named tuple type enabling to access outputs by attributes (e.g. out.x).
            output_class_name = f'{self.__class__.__name__}Output'
            result_type = namedtuple(typename=output_class_name, field_names=self._bound_outputs.keys())

            # Return the "default" bound output ports.
            results = result_type(*self._bound_outputs.values())

        # Return the results.
        return results

    @property
    def input_ports(self) -> Optional[Dict[str, NeuralType]]:
        """Returns definitions of module input ports.

        Returns:
          A (dict) of module's input ports names to NeuralTypes mapping
        """
        return self._bound_input_ports

    @property
    def output_ports(self):
        """
            Returns module output ports.

        .. note::
            This method is NOT returning the dictionary with definitions (like Neural Module),
            but the OutputPorts object. This was required to enable user to override the "default bound outputs"
            with classical __setitem__ statement.


        Returns:
            A module output ports object.
            
        """
        return self._bound_outputs

    @property
    def operation_mode(self):
        """ Returns operation mode. """
        return self._operation_mode

    def __enter__(self):
        """ 
            Activates this graph.
        
            Returns:
                The graph object.
        """
        self._app_state.active_graph = self
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        """
            Deactivates the current graph.
        """
        self._app_state.active_graph = None

    def activate(self):
        """ 
            Activates this graph.
        """
        self._app_state.active_graph = self

    def deactivate(self):
        """
            Deactivates the current graph.
        """
        self._app_state.active_graph = None

    def __str__(self):
        """ Prints a nice summary. """
        # TODO: a nice summary. ;)
        desc = "`{}` ({}):\n".format(self.name, len(self._steps))
        for op in self._steps:
            desc = desc + "  {}\n".format(type(op[0]).__name__)
        return desc

    def __getitem__(self, key):
        """ Returns module given its name (name of the variable).

            Args:
                key: Name of the variable.
        """
        if key not in self._modules.keys():
            raise KeyError("Neural Graph doesn't contain a module named {}".format(key))
        return self._modules[key]

    def __len__(self):
        return len(self._modules)

    def list_modules(self):
        desc = "{} ({}):\n".format(self.name, len(self))
        for key, value in self._modules.items():
            desc += " * `{}` ({})\n".format(key, value)
        return desc

    def record_step(self, module, inputs):
        """
            Records the operation (module plus passed inputs) on a list.
        """
        # Check if module with that name already exists.
        # if module.name in self._modules.keys():
        #    raise KeyError("Neural Graph already contains a module named {}".format(module.name))
        # Add module to list of modules.
        self._modules[module.name] = module

        # Add step.
        self._steps.append([module, inputs])

    def bind_input(self, port_name, port_definition, bound_module):
        # print("Binding input: `{}`: def = `{}` value = NONE".format(port_name, port_definition))
        # Copy the definition of the port to graph input port definition.
        self._bound_input_ports[port_name] = port_definition

        # Indicate that this tensor is missing and has to be provided!
        self._bound_input_tensors[port_name] = None
        # Additionally, remember the bound module
        self._bound_input_modules[port_name] = bound_module

    def bind_default_outputs(self, tensors_list):
        """ Binds default outputs.

            Args:
                tensors_list: List of tensors to be added.
        """
        self._bound_outputs.bind_defaults(tensors_list)

    def show_bound_inputs(self):
        print("bound input ports: ")
        for key, value in self._bound_input_ports.items():
            print(" * `{}`: `{}` ({})".format(key, value, type(value)))

        print("bound input tensors: ")
        for key, value in self._bound_input_tensors.items():
            print(" * `{}`: `{}` ({})".format(key, value, type(value)))

    def show_bound_outputs(self):
        print("bound (default) output ports: ")
        for key, value in self._bound_output_ports_default.items():
            print(" * `{}`: `{}` ({})".format(key, value, type(value)))

        print("bound (default) output tensors: ")
        for key, value in self._bound_output_tensors_default.items():
            print(" * `{}`: `{}` ({})".format(key, value, type(value)))
