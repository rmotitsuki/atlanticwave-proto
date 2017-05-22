# Copyright 2016 - Sean Donovan
# AtlanticWave/SDX Project


from shared.LCFields import *

class LCActionTypeError(TypeError):
    pass
    
class LCActionValueError(ValueError):
    pass

class LCAction(object):
    ''' This is the parent class of actions that the SDXLCRules use. '''

    def __init__(self, name):
        self._name = name


    def __str__(self):
        # Default only works for boring actions.
        retstr = "%s" % (self._name)
        return retstr

    def __repr__(self):
        return self.__str__()


class Forward(LCAction):
    ''' This forwards packets to a particular location. '''
    def __init__(self, port):
        self.port = port
        super(Forward, self).__init__("Forward")

    def __str__(self):
        retstr = "%s:%s" % (self._name, self.port)
        return retstr

    def get(self):
        return self.port

class SetField(LCAction):
    ''' Sets a field in a packet. '''
    def __init__(self, field):
        self.field = field
        super(SetField, self).__init__("SetField")

    def __str__(self):
        retstr = "%s:%s" % (self._name, self.field)
        return retstr

    def get(self):
        return self.field

class SetMetadata(LCAction):
    ''' Sets the metadata meta-field. '''
    def __init__(self, value, mask=2**64-1):
        self.value = value
        self.mask = mask
        super(SetMetadata, self).__init__("SetMetadata")

    def __str__(self):
        retstr = "%s:%s mask %s" % (self._name, self.value, self.mask)

    def get(self):
        # Returns as tuple (value, mask)
        return (self.value, self.mask)

class Continue(LCAction):
    ''' Continues on to the next table. '''
    def __init__(self):
        super(Continue, self).__init__("Continue")

class Drop(LCAction):
    ''' Drop the packets. '''
    def __init__(self):
        super(Drop, self).__init__("Drop")
