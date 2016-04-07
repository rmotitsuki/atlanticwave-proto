# Copyright 2016 - Sean Donovan
# AtlanticWave/SDX Project


from shared.ofconstants import *
from shared.offield import *
from shared.action import OpenFlowAction

class OpenFlowInstructionTypeError(TypeError):
    pass

class OpenFlowInstructionValueError(ValueError):
    pass

class OpenFlowInstruction(object):
    ''' This is the parent class for all OpenFlow Instructions. It will include
        much of the functionality built-in that is necessary to validate most
        instructions.
        Subclasses will need to fill in certain values defined in __init__()
        which will often be enough for the existing validation routines. '''
    # Note that it's a child of OpenFlowAction. It gets check_validity()
    # functions for free.

    def __init__(self, actions, name="OpenFlowInstruction"):
        ''' fields is a list of OpenFlowActions '''
        if type(actions) != type([]):
            raise OpenFlowInstructionTypeError("actions must be a list")
        for entry in actions:
            if not isinstance(entry, Field) and not isinstance(entry, OpenFlowAction):
                raise OpenFlowInstructionTypeError(
                    "actions must be a list of Field or OpenFlowAction objects")
        self.actions = actions
        self._name = name

    def __repr__(self):
        actionstr = ""
        for entry in self.actions:
            actionstr = entry.__repr__() + ",\n"
        if actionstr != "":
            actionstr = actionstr[0:-2]
        return "%s : %s" % (self.__class__.__name__,
                            actionstr)

    def __str__(self):
        actionstr = ""
        for entry in self.actions:
            actionstr += str(entry) + ", "
        if actionstr != "":
            actionstr = actionstr[0:-2]
        return "%s(%s)" % (self._name, actionstr)

    def check_validity(self):
        for action in self.actions:
            if not action.is_optional(self.actions):
                action.check_validity()
            action.check_prerequisites(self.actions)

            
class instruction_GOTO_TABLE(OpenFlowInstruction):
    ''' This instruction pushes matching flows to a specified table. '''

    def __init__(self, tableid=None):
        self.table_id = number_field('table_id', minval=OF_TABLE_MIN,
                                     maxval=OF_TABLE_MAX, value=tableid)
        super(instruction_GOTO_TABLE, self).__init__([self.table_id],
                                                     "goto")        


class instruction_WRITE_METADATA(OpenFlowInstruction):
    ''' This instruction writes metadata information to matching flows. '''

    def __init__(self, metadata, metadata_mask=0xFFFFFFFFFFFFFFFF):
        self.metadata = number_field('metadata', minval=1,
                                     maxval=2**64-1,
                                     value=metadata)
        self.metadata_mask = number_field('metadata_mask', minval=1,
                                          maxval=2**64-1,
                                          value=metadata_mask)
        super(instruction_WRITE_METADATA, self).__init__([self.metadata,
                                                          self.metadata_mask],
                                                         "write_metadata")

class instruction_WRITE_ACTIONS(OpenFlowInstruction):
    ''' This instruction writes actions. '''

    def __init__(self, actionlist):
        self.actions = actionlist
        if type(self.actions) != type([]):
            raise OpenFlowInstructionTypeError("actions must be a list")
        for entry in self.actions:
            if not isinstance(entry, OpenFlowAction):
                raise OpenFlowInstructionTypeError(
                    "actions must be a list of OpenFlowAction objects")
        super(instruction_WRITE_ACTIONS, self).__init__(actionlist,
                                                        "write_actions")


class instruction_APPLY_ACTIONS(OpenFlowInstruction):
    ''' This instruction applies actions. '''

    def __init__(self, actionlist):
        self.actions = actionlist
        super(instruction_APPLY_ACTIONS, self).__init__(actionlist,
                                                        "apply_actions")
        
class instruction_CLEAR_ACTIONS(OpenFlowInstruction):
    ''' This instruction clears actions. '''

    def __init__(self):
        super(instruction_CLEAR_ACTIONS, self).__init__([],
                                                        "clear_actions")


# TODO - OFPIT_METER, page 55,57 of OF 1.3.2 spec.
