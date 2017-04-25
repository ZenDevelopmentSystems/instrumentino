from kivy.properties import ObjectProperty, ListProperty
from kivy.event import EventDispatcher
from construct.core import Struct, Int8ub, Int16ub, Int32ub, Int64ub, PrefixedArray, Enum, Const, Union, GreedyString, \
    Prefixed
from construct.lib.container import Container
import time
import queue
from kivy.app import App
from instrumentino.cfg import *


class ControlinoProtocol(EventDispatcher):
    '''The communication protocol used to interface hardware controllers running controlino.
    The Controlino protocol assumes some sort of serial communication, and that packets arrive in the right order. 
    '''

    DEFAULT_DATA_PACKET_RATE = 10
    '''10 Hz is fast enough for a smooth GUI.
    '''

    controller = ObjectProperty(None)
    '''The controller for which this protocol is used.
    '''

    string_packets_queue = queue.Queue(maxsize=10)
    '''String packets arrive as a response to specific commands (such as 'PING').
    This queue lets the command sender be notified on the reply's arrival.
    Items in the queue are strings, that are compared to the incoming string packets' first word.
    '''

    '''---Outgoing packets---'''

    CMD_ACQUIRE_START = 'ACQUIRE:START'
    '''A command for synchronizing times between Instrumentino and a controller, in order to start an acquisition block.
    Instrumentino sends this packet once to each controller and the time it was sent is considered t_zero from then on,
    and all of the timestamps sent by the controller are relative to this t_zero.  
    '''

    CMD_ACQUIRE_STOP = 'ACQUIRE:STOP'
    '''Stop the current acquisition block.  
    '''

    CMD_PING = 'PING'
    '''A command to check if the controller is responsive.
    '''

    CMD_CHANNEL_DIRECTION = 'CH:DIR'
    CHANNEL_DIRECTION_IN = 'IN'
    CHANNEL_DIRECTION_OUT = 'OUT'
    '''Set the direction of a channel (IN/OUT). 
    '''

    CMD_CHANNEL_REGISTER = 'CH:REGISTER'
    '''Tell the controller that we want to start receiving data from a certain pin in a certain frequency.
    '''

    CMD_CHANNEL_WRITE = 'CH:WRITE'
    '''Write data to a channel. 
    '''

    '''---Incoming packets---'''

    STRING_PACKET_TYPE_PONG = 'PONG'
    '''The string reply to the 'PING' command
    '''

    CONST_HEADER = b'\xA5\xA5\xA5\xA5'
    '''Used for synchronization, to know when a packet starts.
    '''

    incoming_packet_types = {'DATA_PACKET': 0,
                             'STRING_PACKET': 1}
    '''Data packets are constantly expected to come from the controller.
    String packets are used for replying specific commands (such as 'PING'), and are not expected so often.
    '''

    packet_header_format = Struct(const_header=Const(CONST_HEADER),
                                  type=Enum(Int8ub, **incoming_packet_types),
                                  packet_length=Int16ub
                                  )
    '''A header for all packets sent from controllers to Instrumentino.
    The first 4 bytes of a data packet consist of a pre-defined sequence in order to identify the packet' beginning.
    Then comes a packet type field, followed by the total length of the packet.
    The rest is packet specific. 
    '''

    data_packet_header_format = Struct(relative_start_timestamp=Int32ub)
    '''A header for a data packet. Contains the timestamp for the first datapoint in the packet, relative to t_zero. 
    '''

    data_packet_data_block_format = Struct(block_id=Int8ub, data=Union(Int8=Prefixed(Int16ub, Int8ub[:]),
                                                                       Int16=Prefixed(Int16ub, Int16ub[:]),
                                                                       Int32=Prefixed(Int16ub, Int32ub[:]),
                                                                       Int64=Prefixed(Int16ub, Int64ub[:]),
                                                                       parsefrom='Int8'
                                                                       ))
    '''A data block in a data packet
    '''

    data_packet_format = Struct(packet_header=packet_header_format,
                                data_packet_header=data_packet_header_format,
                                data_blocks=PrefixedArray(lengthfield=Int8ub, subcon=data_packet_data_block_format)
                                )
    '''A data packet (Controller->Instrumentino) has the following form (with sizes in bytes):
    [const_header, 4][type, 1][packet_length, 2]    <- general packet header
    [relative_start_timestamp, 4]                   <- data packet header 
    [blocks_num, 1] 
    [block1_id, 1][block1_length, 2][block1_data, ?]
    [block2_id, 1][block2_length, 2][block2_data, ?] ...
    
    Timestamps are measured in milliseconds since the last time zeroing.
    '''

    string_packet_format = Struct(packet_header=packet_header_format,
                                  string=GreedyString('utf8')
                                  )
    '''A string packet (Controller->Instrumentino) has the following form (with sizes in bytes):
    [const_header, 4][type, 1][packet_length, 2]    <- general packet header
    [string without termination, ?]
    '''

    def __init__(self, **kwargs):
        check_for_necessary_attributes(self, ['controller'], kwargs)
        super(ControlinoProtocol, self).__init__(**kwargs)


    def handle_incoming_bytes(self, incoming_bytes):
        '''Parse incoming bytes into packets and act upon them.
        '''

        if DEBUG_RX:
            print('RX: {} ({})'.format(''.join('{:02X}'.format(x) for x in incoming_bytes),
                                       ''.join(chr(x) if chr(x).isalnum() else '.' for x in incoming_bytes)))

        if len(incoming_bytes) > self.controller.comm_port.MAX_BYTES_PER_READ:
            # Communication port overloaded so disconnect.
            #TODO: Is this the right thing to do? Better try to recover.
            self.controller.disconnect()

        # Parse all of the packets in the incoming buffer
        while True:
            packet_start = incoming_bytes.find(self.CONST_HEADER)
            if packet_start == -1:
                # Delete garbage incoming data
                incoming_bytes[:] = []
                return
            else:
                # Delete bytes before the current packet
                incoming_bytes[:packet_start] = []

            if len(incoming_bytes) < self.packet_header_format.sizeof():
                # Not enough bytes for a packet header
                return

            # Parse packet's header and check if we got the whole packet
            packet_header = self.packet_header_format.parse(incoming_bytes)
            packet_end = packet_start + packet_header.packet_length
            if len(incoming_bytes) < packet_end:
                return

            # Act upon the incoming packet and remove it from the incoming buffer
            packet = incoming_bytes[packet_start:packet_end]
            incoming_bytes[:packet_end] = []
            if packet_header.type == 'DATA_PACKET':
                # Parse the new data packet
                self.controller.update_input_channels(self.data_packet_format.parse(packet))
            elif packet_header.type == 'STRING_PACKET':
                # Add the incoming string packet to the string packets queue
                self.string_packets_queue.put(self.string_packet_format.parse(packet))
            else:
                # Delete unrecognized incoming packet
                incoming_bytes[:packet_end] = []

    def build_command_packet(self, command, parameters=[]):
        '''Build a command packet to be sent to a controller
        Command packets have the form: [command] [param1] [param2] ... \r
        '''
        return ' '.join('{}'.format(x) for x in ([command] + parameters)) + '\r'

    def ping(self):
        '''Send a PING command to the controller, and wait for the reply. 
        '''
        # Build and send the ping command
        packet = self.build_command_packet(self.CMD_PING)
        self.transmit(packet)

        # TODO: remove this when the issue is resolved (the receive scheduled calls only start after the on_config_change is complete)
        self.controller.comm_port.receive();

        # Wait for the reply
        try:
            reply_packet = self.string_packets_queue.get(timeout=(1 / self.DEFAULT_DATA_PACKET_RATE * 2))
            if reply_packet:
                self.string_packets_queue.task_done()
        except:
            # The reply didn't arrive in time
            return False

        # Parse the reply packet and check if it arrived correctly
        reply_string = reply_packet.search('string')
        return reply_string == self.STRING_PACKET_TYPE_PONG

    def register_input_channel(self, channel):
        '''Ask controller to start sending us data for a channel.
        '''
        packet = self.build_command_packet(self.CMD_CHANNEL_REGISTER,
                                           [channel.get_identifier(), str(channel.sampling_rate)])
        self.transmit(packet)

    def set_channel_direction(self, channel, is_output):
        '''Set the channel's direction to be input/output
        '''
        packet = self.build_command_packet(self.CMD_CHANNEL_DIRECTION, [channel.get_identifier(),
                                                                        self.CHANNEL_DIRECTION_OUT if is_output else self.CHANNEL_DIRECTION_IN])
        self.transmit(packet)

    def write_to_channel(self, channel, values):
        '''Write values to an output channel.
        values must be given as an array
        '''
        packet = self.build_command_packet(self.CMD_CHANNEL_WRITE, [channel.get_identifier()] + values)
        self.transmit(packet)

    def start_acquiring_data(self):
        '''Set t=0 to now, so we have a reference point for the rest of the timestamps exchanged between Instrumentino and the controller.
        This signals the controller to start sending us data packets.
        '''
        packet = self.build_command_packet(self.CMD_ACQUIRE_START)
        self.transmit(packet)

    def transmit(self, packet):
        '''Transmit a packet through the communication port, if possible
        '''
        if self.controller.comm_port is not None:
            self.controller.comm_port.transmit(packet)