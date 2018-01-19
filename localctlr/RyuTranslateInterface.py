# Copyright 2016 - Sean Donovan
# AtlanticWave/SDX Project


import logging
import threading
import dataset
import cPickle as pickle
import requests
import json
from time import sleep

# Generic AtlanticWave/SDX imports
from shared.LCAction import *
from shared.LCFields import *
from shared.LCRule import *
from shared.ofconstants import *
from oftables import *
from InterRyuControllerConnectionManager import *

# Ryu libraries
from ryu import cfg
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.utils import hex_array
from ryu.lib.packet import packet, ethernet, ether_types

# LC Rule Types
from shared.MatchActionLCRule import *
from shared.VlanTunnelLCRule import *
from shared.LearnedDestinationLCRule import *
from shared.EdgePortLCRule import *
from shared.L2MultipointEndpointLCRule import *
from shared.L2MultipointFloodLCRule import *
from shared.L2MultipointLearnedDestinationLCRule import *
from shared.FloodTreeLCRule import *


LOCALHOST = "127.0.0.1"


CONF = cfg.CONF

class TranslatedRuleContainer(object):
    ''' Parent class for holding both LC and Corsa rules '''
    pass

class TranslatedLCRuleContainer(TranslatedRuleContainer):
    ''' Used by RyuTranslateInterface to track translations of LCRules. Contains
        Ryu-friendly objects. Not for use outside RyuTranslateInterface. '''
    def __init__(self, cookie, table, priority, match, instructions,
                 buffer_id=None, idle_timeout=0, hard_timeout=0):
        self.cookie = cookie
        self.table = table
        self.priority = priority
        self.match = match
        self.instructions = instructions
        self.buffer_id = buffer_id
        self.idle_timeout = idle_timeout
        self.hard_timeout = hard_timeout

    def __str__(self):
        return "%s:%s:%s\n%s\n%s\n%s:%s:%s" % (self.cookie, self.table,
                                               self.priority, self.match,
                                               self.instructions, 
                                               self.buffer_id,
                                               self.idle_timeout,
                                               self.hard_timeout)

    def get_cookie(self):
        return self.cookie

    def get_table(self):
        return self.table
    
    def get_priority(self):
        return self.priority
    
    def get_match(self):
        return self.match
    
    def get_instructions(self):
        return self.instructions
    
    def get_buffer_id(self):
        return self.buffer_id
    
    def get_idle_timeout(self):
        return self.idle_timeout
    
    def get_hard_timeout(self):
        return self.hard_timeout

    
class TranslatedCorsaRuleContainer(TranslatedRuleContainer):
    ''' Used by RyuTranslateInterface to track translations of Corsa Rules.
        Contains what is needed to make a REST request. '''
    def __init__(self, function, url, json, token, list_of_valid_responses):
        self.function = function
        self.url = url
        self.json = json
        self.token = token
        self.valid_responses = list_of_valid_responses

    def __str__(self):
        return "%s:%s:%s:%s" % (self.url, self.json,
                                self.valid_responses, self.token)

    def get_function(self):
        # Function should be "patch", "post", or "get"
        return self.function
    
    def get_url(self):
        return self.url

    def get_json(self):
        return self.json

    def get_token(self):
        return self.token

    def get_valid_responses(self):
        return self.valid_responses
    
class GotoTable(LCAction):
    ''' This performs a goto table instruction in OpenFlow. 
        This is not part of shared/LCAction.py because we don't want the 
        SDXController-level rules to use it, thus it's here. It's very similar
        to all the other LCActions. '''
    def __init__(self, table):
        self.table = table
        super(GotoTable, self)._-init__("GotoTable")

    def __str__(self):
        retstr = "%s:%s" % (self._name, self.table)
        return retstr

    def get(self):
        return self.table

class RyuTranslateInterface(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(RyuTranslateInterface, self).__init__(*args, **kwargs)

        self._setup_logger()

        # Configuration file + parsing
        self.name = CONF['atlanticwave']['lcname']
        self.conf_file = CONF['atlanticwave']['conffile']
        self._import_configuration()


        # Start up Database connection
        # DB is in-memory, as this probalby doesn't need to be tracked through
        # reboots. details on the setup:
        # https://dataset.readthedocs.io/en/latest/api.html
        # https://github.com/g2p/bedup/issues/38#issuecomment-43703630
        # https://www.sqlite.org/inmemorydb.html
        # FIXME: May need to reconsider this in the future, especially for
        # optimization (reducing translations is a good step in optimizing).
        dblocation = "sqlite:///:memory:"
        self.db = dataset.connect(dblocation,
                                  engine_kwargs={'connect_args':
                                                 {'check_same_thread':False}})
        # Database Tables
        self.rule_table = self.db['rules']

        #FIXME: Do I want to mirror the RuleManager's config_table?
        

        # Establish connection to RyuControllerInterface
        self.inter_cm = InterRyuControllerConnectionManager()
        self.inter_cm_cxn = self.inter_cm.open_outbound_connection(self.lcip,
                                                                   self.ryu_cxn_port)

        self.datapaths = {}
        self.current_of_cookie = 0

        # Spawn main_loop thread
        self.loop_thread = threading.Thread(target=self.main_loop)
        self.loop_thread.daemon = True
        self.loop_thread.start()

        # Start up the connection to switch?

        # PacketIn callback structure setup
        self.packet_in_cbs = {}


        # TODO: Reestablish connection? Do I have to do anything?
        
        pass


    def _setup_logger(self):
        ''' Internal function for setting up the logger formats. '''
        # This is from LocalController
        # reused from https://github.com/sdonovan1985/netassay-ryu/blob/master/base/mcm.py
        formatter = logging.Formatter('%(asctime)s %(name)-12s: %(levelname)-8s %(message)s')
        console = logging.StreamHandler()
        console.setLevel(logging.WARNING)
        console.setFormatter(formatter)
        logfile = logging.FileHandler('localcontroller.log')
        logfile.setLevel(logging.DEBUG)
        logfile.setFormatter(formatter)
        self.logger = logging.getLogger('localcontroller.ryutranslateinterface')
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(console)
        self.logger.addHandler(logfile)

    def _import_configuration(self):
        ''' Imports configuration parameters from the passed in configuration
            file. '''
        with open(self.conf_file) as data_file:
            data = json.load(data_file)

        # Look at information under the self.name entry, then look at only
        # data relevant to us.
        lcdata = data['localcontrollers'][self.name]
        self.lcip = lcdata['lcip']
        
        ofdata = lcdata['internalconfig']
        self.ryu_cxn_port = ofdata['ryucxninternalport']

        # Get the DPID to name of the various switches this LC controls
        self.dpid_data = {}
        for entry in lcdata['switchinfo']:
            dpid = str(entry['dpid'])
            self.dpid_data[dpid] = {}
            self.dpid_data[dpid]['name'] = entry['name']
            print entry['internalconfig']
            self.dpid_data[dpid]['internalconfig'] = entry['internalconfig']

    def _get_switch_internal_config(self, datapath):
        ''' Gets switch internal config information based on datapath passed in
        '''
        dpid = str(datapath.id)

        import pprint
        pp = pprint.PrettyPrinter(indent=4)
        pp.pprint(self.dpid_data)

        if dpid in self.dpid_data.keys():
            return self.dpid_data[dpid]['internalconfig']
        raise ValueError("%s is not in the dpid_data: %s" % (dpid,
                                                    self.dpid_data.keys()))

    def main_loop(self):
        ''' This is the main loop that reads and works with the data coming from
            the Inter-Ryu Connection. It loops through, looking for new events. 
            If there is one to be processed, process it. 
        '''

        # First, wait till we have at least one datapath.
        self.logger.info("Looking for datapath")
        while len(self.datapaths.keys()) == 0:
            self.logger.info("Waiting " + str(self.datapaths))
            sleep(1)

        # Send message over to the Controller Interface to let it know that
        # we have at least one switch.
        self.inter_cm_cxn.send_cmd(ICX_DATAPATHS,
                                   str(self.datapaths))
        

        while True:

            # FIXME - This is static: only installing rules right now.
            event_type, event_data = self.inter_cm_cxn.recv_cmd()
            (switch_id, event) = event_data
            if switch_id not in self.datapaths.keys():
                # FIXME - Need to update this for sending errors back
                continue
                
            datapath = self.datapaths[switch_id]
            
            if event_type == ICX_ADD:
                self.install_rule(datapath, event)
            elif event_type == ICX_REMOVE:
                self.remove_rule(datapath, event)
            sleep(0.1)

            # FIXME - There may need to be more options here. This is just a start.

    # Handles switch connect event
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        self.logger.warning("Connection from: " + str(ev.msg.datapath.id) + " for " + str(self))
        self.datapaths[ev.msg.datapath.id] = ev.msg.datapath

        # Call bootstrapping for switch functions
        self._new_switch_bootstrapping(ev)

    # From the Ryu mailing list: https://sourceforge.net/p/ryu/mailman/message/33584125/
    @set_ev_cls(ofp_event.EventOFPErrorMsg,
                [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def error_msg_handler(self, ev):
        msg = ev.msg
        self.logger.error('OFPErrorMsg received: type=0x%02x code=0x%02x '
                          'message=%s',
                          msg.type, msg.code, hex_array(msg.data))

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        # Look through the packet_in_cbs's dictionary and send it onwards.
        cookie = ev.msg.cookie

        if cookie in self.packet_in_cbs:
            cb = self.packet_in_cbs[cookie]
            cb(ev)
        else:
            self.logger.error('Packet-in with cookie 0x%02x has no callback.',
                              cookie)


    def _new_switch_bootstrapping(self, ev):
        ''' This bootstraps new switches when they come online. '''
        # Install default rules on all tables
        # For ALL tables except the last table:
        #   - Create a MatchActionLCRule to send to next table. Priority 0
        # Learning table edge ports are handled by rules coming from the
        # SDX controller at startup.
        switch_id = 0  # This is unimportant:
                       # it's never used in the translation
        datapath = ev.msg.datapath
        of_cookie = self._get_new_OF_cookie(-1) #FIXME: magic number
        results = []
        for table in ALL_TABLES_EXCEPT_LAST:
            matches = [] # FIXME: what's the equivalent of match(*)?
            actions = [Continue()]
            priority = PRIORITY_DEFAULT
            marule = MatchActionLCRule(switch_id, matches, actions)
            results += self._translate_MatchActionLCRule(datapath,
                                                         table,
                                                         of_cookie,
                                                         marule,
                                                         priority)

        # For last table
        #   - Create a default drop rule (if necessary needed). Priority 0
        matches = []
        actions = [Drop()]
        priorty = PRIORITY_DEFAULT
        table = LASTTABLE
        marule = MatchActionLCRule(switch_id, matches, actions)
        results += self._translate_MatchActionLCRule(datapath,
                                                     table,
                                                     of_cookie,
                                                     marule,
                                                     priority)
        # Install default rules
        for rule in results:
            self.add_flow(datapath, rule)
                            
        
        #FIXME: in-band communication
    
    def _translate_MatchActionLCRule(self, datapath, table,
                                     of_cookie, marule, priority=100):
        ''' This translates MatchActionLCRules. There is only one rule generated
            by any given MatchActionLCRule. 
            Returns a list of TranslatedLCRuleContainers
        ''' 
        results = []

        # Translate all the pieces
        match = self._translate_LCMatch(datapath,
                                        marule.get_matches(),
                                        table)
        instructions = self._translate_LCAction(datapath,
                                                marule.get_actions(),
                                                table)

        # Make the TranslatedRuleContainer, and return it.
        trc = TranslatedLCRuleContainer(of_cookie, table, priority,
                                        match, instructions)
        results.append(trc)

        return results

    
    def _translate_VlanLCRule(self, datapath, table, of_cookie, vlanrule):
        ''' This translates VlanLCRules. This can generate one or two rules, 
            depending on if this is a bidirectional tunnel (the norm) or not.
            Returns a list of TranslatedLCRuleContainers
        '''
        results = []
        internal_config = self._get_switch_internal_config(datapath)

        # Create Outbound Rule
        # There are two options here: Corsa or Non-Corsa. Non-Corsa is for
        # regular OpenFlow switches (such as OVS) and is more straight forward.

        if internal_config['corsaurl'] == "":
            # Make the equivalent MatchActionLCRule, translate it, and use these
            # as the results. Easier translation!
            switch_id = 0  # This is unimportant:
                           # it's never used in the translation
            matches = [IN_PORT(vlanrule.get_inport()),
                       VLAN_VID(vlanrule.get_vlan_in())]
            actions = [SetField(VLAN_VID(vlanrule.get_vlan_out())),
                       Forward(vlanrule.get_outport())]
            marule = MatchActionLCRule(switch_id, matches, actions)
            results += self._translate_MatchActionLCRule(datapath,
                                                         table,
                                                         of_cookie,
                                                         marule)

            # If bidirectional, create inbound rule
            if vlanrule.get_bidirectional() == True:
                matches = [IN_PORT(vlanrule.get_outport()),
                           VLAN_VID(vlanrule.get_vlan_out())]
                actions = [SetField(VLAN_VID(vlanrule.get_vlan_in())),
                           Forward(vlanrule.get_inport())]
                marule = MatchActionLCRule(switch_id, matches, actions)
                results += self._translate_MatchActionLCRule(datapath,
                                                             table,
                                                             of_cookie,
                                                             marule)

        else:
            # Corsa case is more complicated.
            # 4 OpenFlow rules needed:
            #   - Inbound port  on VLAN in  to BW-in-port    on VLAN out
            #   - BW-out-port   on VLAN out to Outbound port on VLAN out
            #   - BW-in-port    on VLAN out to Inbound port  on VLAN in 
            #   - Outbound port on VLAN out to BW-out-port   on VLAN out

            # 1 Bandwidth Reservation REST rule needed
            #   - Set Bandwith information for the tunnel

            # OpenFlow rules are *very* similar to the non-Corsa case
            switch_id = 0  # This is unimportant:
                           # it's never used in the translation
            matches = [IN_PORT(vlanrule.get_inport()),
                       VLAN_VID(vlanrule.get_vlan_in())]
            actions = [SetField(VLAN_VID(vlanrule.get_vlan_out())),
                       Forward(internal_config['corsabwin'])]
            marule = MatchActionLCRule(switch_id, matches, actions)
            results += self._translate_MatchActionLCRule(datapath,
                                                         table,
                                                         of_cookie,
                                                         marule)

            matches = [IN_PORT(internal_config['corsabwout']),
                       VLAN_VID(vlanrule.get_vlan_out())]
            actions = [SetField(VLAN_VID(vlanrule.get_vlan_out())),
                       Forward(vlanrule.get_outport())]
            marule = MatchActionLCRule(switch_id, matches, actions)
            results += self._translate_MatchActionLCRule(datapath,
                                                         table,
                                                         of_cookie,
                                                         marule)

            # If bidirectional, create inbound rule
            if vlanrule.get_bidirectional() == True:
                matches = [IN_PORT(internal_config['corsabwin']),
                           VLAN_VID(vlanrule.get_vlan_out())]
                actions = [SetField(VLAN_VID(vlanrule.get_vlan_in())),
                           Forward(vlanrule.get_inport())]
                marule = MatchActionLCRule(switch_id, matches, actions)
                results += self._translate_MatchActionLCRule(datapath,
                                                             table,
                                                             of_cookie,
                                                             marule)
                
                matches = [IN_PORT(vlanrule.get_outport()),
                           VLAN_VID(vlanrule.get_vlan_out())]
                actions = [SetField(VLAN_VID(vlanrule.get_vlan_out())),
                           Forward(internal_config['corsa_bw_out'])]
                marule = MatchActionLCRule(switch_id, matches, actions)
                results += self._translate_MatchActionLCRule(datapath,
                                                             table,
                                                             of_cookie,
                                                             marule)


            # Bandwidth REST rules rely on the REST API. If it changes, then
            # this may need to be modified.
            bridge = internal_config['corsaratelimitbridge']
            vlan = vlanrule.get_vlan_out()
            bandwidth = vlanrule.get_bandwidth()

            #Find out the request_url
            tunnel_url = (internal_config['corsaurl'] + "api/v1/bridges/" +
                          bridge + "/tunnels?list=true")
            print "Requesting tunnels from %s" % tunnel_url
            rest_return = requests.get(tunnel_url,
                                       headers={'Authorization':
                                                internal_config['corsatoken']},
                                       verify=False) #FIXME: HARDCODED

            print "Looking for %s on ports %s" % (vlan,
                                      internal_config['corsaratelimitports'])
                
            for entry in rest_return.json()['list']:
                if (entry['vlan-id'] == vlan and
                    int(entry['port']) in internal_config['corsaratelimitports']):

                    request_url = entry['links']['self']['href']
                    # This implements Red/Green, per Corsa's spec. Anything over
                    # the CIR value (and not part of a CBS burst) will be marked
                    # red and dropped.
                    jsonval = [{'op':'replace',
                                'path':'/meter/cir',
                                'value':bandwidth},
                               {'op':'replace',
                                'path':'/meter/cbs',
                                'value':bandwidth},
                               {'op':'replace',
                                'path':'/meter/eir',
                                'value':0},
                               {'op':'replace',
                                'path':'/meter/ebs',
                                'value':0}]
                    valid_responses = [204]

                    print "Patching %s:%s" % (request_url, json)
                    results.append(TranslatedCorsaRuleContainer("patch",
                                                request_url,
                                                jsonval,
                                                internal_config['corsatoken'],
                                                valid_responses))
        
        # Return results to be used.
        return results

    def _translate_LearnedDestinationLCRule(self, datapath, switch_table,
                                            of_cookie, ldrule):
        ''' This translates LearnedDestinationLCRules. This will generate a 
            single rule.
            Returns a list of TranslatedRuleContainers
        '''
        results = []
        switch_id = 0 # This is unimportant: it's never used in the translation
        matches = [ETH_DST(ldrule.get_dst_address())]
        actions = [Forward(ldrule.get_outport())]
        priority = PRIORITY_GENERIC_LEARNED
        marule = MatchActionLCRule(switch_id, matches, actions)
        results += self._translate_MatchActionLCRule(datapath,
                                                     switch_table,
                                                     of_cookie,
                                                     marule,
                                                     priority)
        return results

    def _translate_EdgePortLCRule(self, datapath, switch_table,
                                  of_cookie, eprule):
        ''' This translates EdgePortLCRules. EdgePortLCRules declare that this is
            an edge port, nothing more. This will generate a single rule.
            Returns a list of TranslatedRuleContainers
        '''
        results = []
        switch_id = 0 # This is unimportant: it's never used in the translation
        matches = [IN_PORT(eprule.get_edgeport())]
        actions = [Continue(), Forward(OFPP_CONTROLLER)]
        priority = PRIORITY_GENERIC_LEARNING
        marule = MatchActionLCRule(switch_id, matches, actions)
        results += self._translate_MatchActionLCRule(datapath,
                                                     switch_table,
                                                     of_cookie,
                                                     marule,
                                                     priority)
        return results
    
    def _translate_L2MultipointFloodLCRule(self, datapath, switch_table,
                                           of_cookie, mpfrule):
        ''' This translates L2MultipointFloodLCRules. L2MultipointFloodLCRules 
            are for ports that are on the interior of a Steiner tree that 
            connects L2Multipoint LANs. Endpoint switches use 
            L2MultipointEndpointLCRules instead.
            Returns a list of TranslatedRuleContainers
        '''
        results = []
        switch_id = 0 # This is unimportant: it's never used in the translation

        vlan = mpfrule.get_intermediate_vlan()
        for port in mpfrule.get_flooding_ports():
            matches = [IN_PORT(port), VLAN_VID(vlan)]
            actions = []
            for outport in mpfrule.get_flooding_ports():
                if outport != port:
                    actions.append(Forward(outport))
            priority = PRIORITY_L2M_FLOOD_FORWARDING
            marule = MatchActionLCRule(switch_id, matches, actions) 
            results += self._translate_MatchActionLCRule(datapath,
                                                         switch_table,
                                                         of_cookie,
                                                         marule,
                                                         priority)
        return results

    def _translate_L2MultipointEndpointLCRule(self, datapath,
                                              endpoint_table,
                                              translate_table,
                                              flood_table,
                                              learning_table,
                                              of_cookie, mperule):
        ''' This translates L2MultipointEndpointLCRules. 
            L2MultipointEndpointLCRules are uses for endpoints on a Steiner tree
            connecting L2Multipoint LANs. These handle bandwidth management, 
            VLAN rewriting for edge ports, flooding on the switch that has the 
            endpoint, and learning rule installation.
            Interior switches on the Steiner tree are handled by the 
            L2MultipointFloodLCRule instead, and are much simpler.
            Returns a list of TranslatedRuleContainers
        '''
        results = []
        internal_config = self._get_switch_internal_config(datapath)
        switch_id = 0 # This is unimportant: it's never used in the translation
        intermediate_vlan = mperule.get_intermediate_vlan()

        # Non-Corsa first
        if internal_config['corsaurl'] == "":
            # Endpoint ports
            # - Translate VLANs on ingress on endpoint_table
            # - Install learning rules on intermediate VLAN on ingress on
            #   learning table
            for (port, vlan) in mperule.get_endpoint_ports_and_vlans():
                matches = [IN_PORT(port), VLAN_VID(vlan)]
                actions = [SetField(VLAN_VID(intermediate_vlan)), Continue()]
                priority = PRIORITY_L2MULTIPOINT
                marule = MatchActionLCRule(switch_id, matches, actions)
                results += self._translate_MatchActionLCRule(datapath,
                                                             endpoint_table,
                                                             of_cookie,
                                                             marule,
                                                             priority)

                matches = [IN_PORT(port), VLAN_VID(intermediate_vlan)]
                actions = [Continue(), Forward(OFPP_CONTROLLER)]
                priority = PRIORITY_L2MULTIPOINT_LEARNING
                marule = MatchActionLCRule(switch_id, matches, actions)
                results += self._translate_MatchActionLCRule(datapath,
                                                             learning_table,
                                                             of_cookie,
                                                             marule,
                                                             priority)


            # Endpoint and Flooding ports.
            # - Install flooding rules on flood table
            flooding_ports = mperule.get_flooding_ports()
            endpoint_ports = [port for (port,vlan) in
                              mperule.get_endpoint_ports_and_vlans()]
            ports = flooding_ports + endpoint_ports
            
            for port in ports:
                matches = [IN_PORT(port), VLAN_VID(intermediate_vlan)]
                actions = []
                for outport in flooding_ports:
                    if outport != port:
                        actions.append(Forward(outport))
                for (outport, vlan) in mperule.get_endpoint_ports_and_vlans():
                    if outport != port:
                        actions.append(SetField(VLAN_VID(vlan)))
                        actions.append(Forward(outport))
                priority = PRIORITY_L2M_FLOOD_FORWARDING
                marule = MatchActionLCRule(switch_id, matches, actions)
                results += self._translate_MatchActionLCRule(datapath,
                                                             flood_table,
                                                             of_cookie,
                                                             marule,
                                                             priority)

                matches = [IN_PORT(port), 
                           VLAN_VID(intermediate_vlan), 
                           ETH_DST('ff:ff:ff:ff:ff:ff')]
                # Same actions as above, no need to rebuild
                priority = PRIORITY_L2M_BROADCAST_FORWARDING
                marule = MatchActionLCRule(switch_id, matches, actions)
                results += self._translate_MatchActionLCRule(datapath,
                                                             flood_table,
                                                             of_cookie,
                                                             marule,
                                                             priority)

        # Corsa Case
        else:
            # Endpoint rules
            for (port, vlan) in mperule.get_endpoint_ports_and_vlans():
                # - Ingress rule from endpoints -
                #  - endpoint_table
                #  - match inport(endpoint), vlan(endpoint)
                #   - set current VLAN tag to inport's value
                #   - Push new VLAN tag
                #   - Set new VLAN tag to intermediate
                #   - Forward to bw-in port
                matches = [IN_PORT(port), VLAN_VID(vlan)]
                actions = [SetField(VLAN_VID(port)),
                           PushVLAN(),
                           SetField(VLAN_VID(intermediate_vlan)),
                           Forward(internal_config['corsabwin'])]
                priority = PRIORITY_L2MULTIPOINT
                marule = MatchActionLCRule(switch_id, matches, actions)
                results += self._translate_MatchActionLCRule(datapath,
                                                             endpoint_table,
                                                             of_cookie,
                                                             marule,
                                                             priority)
                
                # - Translate rule
                #  - translate_table
                #  - match metadata(MD_L2M_TRANSLATE), vlan(endpoint)
                #    See "Rule Needed Once", below, as to why this happens
                #   - set metadata(endpoint)
                #   - set VLAN tag to intermediate
                matches = [METADATA(MD_L2M_TRANSLATE),
                           VLAN_VID(port)]
                actions = [WriteMetadata(port),
                           SetFields(VLAN_VID(Intermediate_vlan))]
                priority = PRIORITY_L2MULTIPOINT_TRANSLATE
                marule = MatchActionLCRule(switch_id, matches, actions)
                results += self._translate_MatchActionLCRule(datapath,
                                                             translate_table,
                                                             of_cookie,
                                                             marule,
                                                             priority)
                                
                # - Learning rule
                #  - learning_table
                #   - match metadata(endpoint), vlan(intermediate)
                #    - continue
                #    - Forward to controller
                matches = [METADATA(port),
                           VLAN_VID(intermediate_vlan)]
                actions = [Continue(), Forward(OFPP_CONTROLLER)]
                priority = PRIORITY_L2MULTIPOINT_LEARNING
                marule = MatchActionLCRule(switch_id, matches, actions)
                results += self._translate_MatchActionLCRule(datapath,
                                                             learning_table,
                                                             of_cookie,
                                                             marule,
                                                             priority)

            # Rule Needed Once
            # - Ingress rule from bw-out port (bw has been managed)
            #  - endpoint_table
            #  - match inport(bw-out), vlan(intermediate)
            #   - pop outer VLAN
            #   - set metadata(MD_L2M_TRANSLATE)
            #   - goto translate_table
            matches = [IN_PORT(internal_config['corsabwout']),
                       VLAN_VID(intermediate_vlan)]
            actions = [PopVLAN(),
                       WriteMetadata(MD_L2M_TRANSLATE),
                       GotoTable(translate_table)]
            priority = PRIORITY_L2MULTIPOINT
            marule = MatchActionLCRule(switch_id, matches, actions)
            results += self._translate_MatchActionLCRule(datapath,
                                                         translate_table,
                                                         of_cookie,
                                                         marule,
                                                         priority)

            bridge = internal_config['corsaratelimitbridge']
            vlan = intermediate_vlan
            bandwidth = mperule.get_bandwidth()

            tunnel_url = (internal_config['corsaurl'] + "api/v1/bridges/" +
                          bridge + "/tunnels?list=true")
            rest_return = requests.get(tunnel_url,
                                       headers={'Authorization':
                                                internal_config['corsatoken']},
                                       verify=False) #FIXME: HARDCODED

            # - Corsa BW Management rule
            for entry in rest_return.json()['list']:
                if (entry['vlan-id'] == vlan and
                    int(entry['port']) in internal_config['corsaratelimitports']):

                    request_url = entry['links']['self']['href']
                    jsonval = [{'op':'replace',
                                'path':'/meter/cir',
                                'value':bandwidth},
                               {'op':'replace',
                                'path':'/meter/eir',
                                'value':bandwidth}]
                    valid_responses = [204]

                    print "Patching %s:%s" % (request_url, json)
                    results.append(TranslatedCorsaRuleContainer("patch",
                                                request_url,
                                                jsonval,
                                                internal_config['corsatoken'],
                                                valid_responses))

            # All ports
            flooding_ports = mperule.get_flooding_ports()
            endpoint_ports = [port for (port,vlan) in
                              mperule.get_endpoint_ports_and_vlans()]
            ports = flooding_ports + endpoint_ports
            # - Flooding rules for endpoints
            #  - flood_table
            #   - match metadata(endpoint), vlan(intermediate)
            #   - set vlan to outbound port, fwd
            #    - Repeat for all ports
            # - Flooding rules for flooding ports
            #  - flood_table
            #   - match inport(flood port), vlan(intermediate)
            #   - set vlan to outbound port, fwd
            #    - Repeat for all ports
            for port in ports:
                matches = []
                if port in endpoint_ports:
                    matches = [METADATA(port), VLAN_VID(intermediate_vlan)]
                elif port in flooding_ports:
                    matches = [IN_PORT(port), VLAN_VID(intermediate_vlan)]
                actions = []
                for outport in flooding_ports:
                    if outport != port:
                        actions.append(Forward(outport))
                for (outport, vlan) in mperule.get_endpoint_ports_and_vlans():
                    if outport != port:
                        actions.append(SetField(VLAN_VID(vlan)))
                        actions.append(Forward(outport))
                priority = PRIORITY_L2M_FLOOD_FORWARDING
                marule = MatchActionLCRule(switch_id, matches, actions)
                results += self._translate_MatchActionLCRule(datapath,
                                                             flood_table,
                                                             of_cookie,
                                                             marule,
                                                             priority)

                if port in endpoint_ports:
                    matches = [METADATA(port), 
                               VLAN_VID(intermediate_vlan), 
                               ETH_DST('ff:ff:ff:ff:ff:ff')]
                elif port in flooding_ports:
                    matches = [IN_PORT(port), 
                               VLAN_VID(intermediate_vlan), 
                               ETH_DST('ff:ff:ff:ff:ff:ff')]
                # Same actions as above, no need to rebuild
                priority = PRIORITY_L2M_BROADCAST_FORWARDING
                marule = MatchActionLCRule(switch_id, matches, actions)
                results += self._translate_MatchActionLCRule(datapath,
                                                             flood_table,
                                                             of_cookie,
                                                             marule,
                                                             priority)
            
        return results

    
    def _translate_L2MultipointLearnedDestinationLCRule(self, datapath,
                                                        switch_table, of_cookie,
                                                        ldrule):
        ''' This translates L2MultipointLearnedDestinationLCRules. This will 
            generate one rule. 
            For non-endpoints, this will forward along the intermediate VLAN
            that's being used for the L2MultipointPolicy.
            For endpoints, this will translate VLAN to the destination VLAN, 
            then forward.
        '''
        results = []
        switch_id = 0 # This is unimportant: it's never used in the translation
        matches = [VLAN_VID(ldrule.get_intermediate_vlan)(),
                   ETH_DST(ldrule.get_dst_address())]
        actions = None
        # Non-endpoints
        if ldrule.get_intermediate_vlan() == ldrule.get_out_vlan():
            actions = [Forward(ldrule.get_outport())]
        else:
            actions = [SetField(VLAN_VID(ldrule.get_out_vlan())),
                       Forward(ldrule.get_outport())]
        priority = PRIORITY_L2M_DESTINATION_FORWARDING
        marule = MatchActionLCRule(switch_id, matches, actions)
        results += self._translate_MatchActionLCRule(datapath,
                                                     switch_table,
                                                     of_cookie,
                                                     marule,
                                                     priority)
        return results

    
    def _translate_FloodTreeLCRule(self, datapath, switch_table,
                                   of_cookie, ftrule):
        ''' This translate FloodTreeLCRules. FloodTreeLCRules are for ports on a
            broadcast flood tree, so len(ports) number of rules need to be 
            installed for each FloodTreeLCRule.
            Returns a list of TranslatedRUleContainers
        '''
        results = []
        switch_id = 0 # This is unimportant: it's never used in the translation
        priority = PRIORITY_FLOOD_FORWARDING
        
        ports = ftrule.get_ports()
        
        for port in ports:
            matches = [IN_PORT(port), ETH_DST('ff:ff:ff:ff:ff:ff')]
            actions = []

            # Forward to all other ports
            for dstport in ports:
                if dstport == port:
                    continue
                actions.append(Forward(dstport))
            marule = MatchActionLCRule(switch_id, matches, actions)
            results += self._translate_MatchActionLCRule(datapath,
                                                         switch_table,
                                                         of_cookie,
                                                         marule,
                                                         priority)
        return results
            
                                   
        
    def _translate_LCMatch(self, datapath, matches, table):
        args = {}
        for m in matches:
            # Add match to list
            args[m.get_name()] = m.get()
            # Add the prereqs to the list too
            for prereq in m.get_prereqs():
                if prereq.get_name() in args.keys():
                    pass
                args[prereq.get_name()] = prereq.get()
                #FIXME: If there's a prereq in conflict (i.e., user specified
                # somethign in the same field, there's a problem) raise an
                # error.
                
        return datapath.ofproto_parser.OFPMatch(**args)

    def _translate_LCAction(self, datapath, actions, table):
        ''' This translates the user-level actions into OpenFlow-level Actions
            and instructions. Returns a list of instructions. '''

        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        instructions = []
        aa_results = []
        
        for action in actions:
            # The first fiew action types are pretty easy: they all end up in
            # an OFPIT_APPLY_ACTIONS instruction.
            if isinstance(action, Forward):
                aa_results.append(parser.OFPActionOutput(action.get()))
                continue
            elif isinstance(action, SetField):
                args = {}
                f = action.get()
                args[f.get_name()] = f.get()
                aa_results.append(parser.OFPActionSetField(**args))
                continue
            elif isinstance(action, PushVLAN):
                aa_results.append(parser.OFPActionPushVlan())
                continue
            elif isinstance(action, PopVLAN):
                aa_results.append(parser.OFPActionPushVlan())
                continue
            # If we've gotten this far, that means the next action is *not* a
            # Forward, SetField, PushVLAN, or PopVLAN action, but will use a
            # different Instruction type, so wrap up the existing actions in an
            # APPLY_ACTIONS instruction first.
            # This is a bit dirty and confusing, sadly.
            if len(aa_results) > 0:
                instructions.append(parser.OFPInstructionActions(
                    ofproto.OFPIT_APPLY_ACTIONS, aa_results))
                aa_results = []

            # Drop is different, it should be the only instruction involved with
            # the match and should clear the actions that are installed.
            if isinstance(action, Drop):
                # This is an error!
                if len(actions) > 1:
                    #FIXME: raise an error
                    pass
                # To drop, need to clear actions associated with the match.
                instructions.append(parser.OFPInstructionActions(
                                         ofproto.OFPIT_CLEAR_ACTIONS, []))

            # Continue and GotoTable are a bit different, as they both reference
            # other tables using the OFPIT_GOTO_TABLE instruction.
            elif isinstance(action, Continue):
                # table is the current table, we want to go to the next table
                instructions.append(parser.OFPInstructionGotoTable(table + 1))
            elif isinstance(action, GotoTable):
                instructions.append(parser.OFPInstructionGotoTable(action.get()))
            # WriteMetadata is a separate instruction, so must be handled
            # separetely
            elif isinstance(action, WriteMetadata):
                (value, mask) = action.get()
                aa_results.append(parser.OFPInstructionWriteMetadata(value,
                                                                     mask))

        # Are there any values in aa_results? If so, put them in APPLY_ACTIONS
        # This is for the case where a bunch of simple rules (Forward, SetField,
        # PushVLAN, PopVLAN) are the only rules that exist, and they haven't yet
        # been put into an instruction. 
        if len(aa_results) > 0: 
            instructions.append(parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, aa_results))           
        # Return all the instructions added up
        return instructions


    def corsa_rest_cmd(self, rc):
        ''' Handles sending of REST commands to Corsa Switches. '''
        verify = False #FIXME: Hardcoded
        if rc.get_function() == "get":
            response = requests.get(rc.get_url(),
                                    json=rc.get_json(),
                                    headers={'Authorization':rc.get_token()},
                                    verify=verify)
        elif rc.get_function() == "post":
            response = requests.post(rc.get_url(),
                                      json=rc.get_json(),
                                      headers={'Authorization':rc.get_token()},
                                      verify=verify)
        elif rc.get_function() == "patch":
            response = requests.patch(rc.get_url(),
                                      json=rc.get_json(),
                                      headers={'Authorization':rc.get_token()},
                                      verify=verify)
        else:
            raise ValueError("Function not valid: %s:%s" %
                        (rc.get_function(),
                         rc.get_json()))

        if response.status_code not in rc.get_valid_responses():
            raise Exception("REST command failed %s:%s\n    %s\n    %s" %
                        (rc.get_function(),
                         rc.get_json(),
                         response.status_code,
                         response.json()))

    def add_flow(self, datapath, rc):
        ''' Ease-of-use wrapper for adding flows. ''' 
        parser = datapath.ofproto_parser

        if rc.get_buffer_id() != None:
            mod = parser.OFPFlowMod(datapath=datapath,
                                    cookie=rc.get_cookie(),
                                    table_id=rc.get_table(),
                                    buffer_id=rc.get_buffer_id(),
                                    priority=rc.get_priority(),
                                    match=rc.get_match(),
                                    instructions=rc.get_instructions(),
                                    idle_timeout=rc.get_idle_timeout(), 
                                    hard_timeout=rc.get_hard_timeout())
        else:
            mod = parser.OFPFlowMod(datapath=datapath,
                                    cookie=rc.get_cookie(),
                                    table_id=rc.get_table(),
                                    # No buffer
                                    priority=rc.get_priority(),
                                    match=rc.get_match(),
                                    instructions=rc.get_instructions(),
                                    idle_timeout=rc.get_idle_timeout(), 
                                    hard_timeout=rc.get_hard_timeout())

        datapath.send_msg(mod)

    def remove_flow(self, datapath, rc):
        #BASE ON: https://github.com/sdonovan1985/netassay-ryu/blob/672a31228ab08abe55c19e75afa52490e76cbf77/base/mcm.py#L283
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        command = ofproto.OFPFC_DELETE
        out_group = ofproto.OFPG_ANY
        out_port = ofproto.OFPP_ANY
        
        cookie = rc.get_cookie()
        table = rc.get_table()
        match = rc.get_match()

        mod = parser.OFPFlowMod(datapath=datapath, cookie=cookie, 
                                table_id=table, command=command,
                                out_group=out_group, out_port=out_port,
                                match=match)
        datapath.send_msg(mod)



        

    def install_rule(self, datapath, sdx_rule):
        ''' The main loop calls this to handle adding a new rule.
            This function handles the translation from the SDX-provided rule to
            OpenFlow rules that the switch can actually work with. '''

        # FIXME: this is where translation from LC/SDX interface to the near OF
        #interface should happen
        
        # Verify input
        if not isinstance(sdx_rule, LCRule):
            raise TypeError("lcrule %s is not of type LCRule: %s" %
                            (sdx_rule, type(sdx_rule)))


        # Get a cookie based on the SDX Controller cookie
        of_cookie = self._get_new_OF_cookie(sdx_rule.get_cookie())
        self.logger.debug("Cookie 0x%02x used for %s" % (of_cookie, sdx_rule))

        # Convert rule into instructions for Ryu. Switch through the different
        # types of supported LCRules for individual translation.
        switch_rules = None
        switch_table = None
        if isinstance(sdx_rule, MatchActionLCRule):
            if sdx_rule.get_ingress == True:
                # Ingress rules are applied right before being sent to the
                # destination network.
                switch_table = SDXINGRESSRULETABLE
            else:
                # Egress rules are applied immediately after leaving the source
                # network.
                switch_table = SDXEGRESSRULETABLE

            switch_rules = self._translate_MatchActionLCRule(datapath,
                                                             switch_table,
                                                             of_cookie,
                                                             sdx_rule)
            
        elif isinstance(sdx_rule, VlanTunnelLCRule):
            # VLAN rules happen before anything else. 
            switch_table = L2TUNNELTABLE
            switch_rules = self._translate_VlanLCRule(datapath,
                                                      switch_table,
                                                      of_cookie,
                                                      sdx_rule)

        elif isinstance(sdx_rule, LearnedDestinationLCRule):
            # Learning switch forwarding rules happen as a fallback at the end
            switch_table = FORWARDINGTABLE
            switch_rules = self._translate_LearnedDestinationLCRule(datapath,
                                                                    switch_table,
                                                                    of_cookie,
                                                                    sdx_rule)
        elif isinstance(sdx_rule, EdgePortLCRule):
            # For bootstrapping for learning. Needs to register a CB as well.
            switch_table = LEARNINGTABLE
            switch_rules = self._translate_EdgePortLCRule(datapath,
                                                          switch_table,
                                                          of_cookie,
                                                          sdx_rule)
            self._register_packet_in_cb(of_cookie, self.unknown_source_cb)
        elif isinstance(sdx_rule, L2MultipointFloodLCRule):
            # Installs 
            switch_table = FORWARDINGTABLE
            self.logger.error("L2MultipointFlood: %d:%d:%s" % (switch_table,
                                                               of_cookie,
                                                               sdx_rule))

            switch_rules = self._translate_L2MultipointFloodLCRule(datapath,
                                                                   switch_table,
                                                                   of_cookie,
                                                                   sdx_rule)
        elif isinstance(sdx_rule, L2MultipointEndpointLCRule):
            # Uses
            endpoint_table = L2TUNNELTABLE
            translate_table = SDXEGRESSRULETABLE
            flood_table = FORWARDINGTABLE
            learning_table = LEARNINGTABLE
            self.logger.error("L2MultipointEndpo: %d,%d:%d:%s" % (
                endpoint_table, flood_table,
                of_cookie,
                sdx_rule))
            switch_rules = self._translate_L2MultipointEndpointLCRule(datapath,
                                                                endpoint_table,
                                                                translate_table,
                                                                flood_table,
                                                                learning_table,
                                                                of_cookie,
                                                                sdx_rule)
            self._register_packet_in_cb(of_cookie,
                                        self.l2multipoint_unknown_source_cb)
        elif isinstance(sdx_rule, L2MultipointLearnedDestinationLCRule):
            # Learning switch forwarding rules happen as a fallback at the end
            switch_table = FORWARDINGTABLE
            switch_rules = self._translate_L2MultipointLearnedDestinationLCRule(
                                     datapath,
                                     switch_table,
                                     of_cookie,
                                     sdx_rule)
            
        elif isinstance(sdx_rule, FloodTreeLCRule):
            switch_table = FORWARDINGTABLE
            switch_rules = self._translate_FloodTreeLCRule(datapath,
                                                           switch_table,
                                                           of_cookie,
                                                           sdx_rule)

        
            

        if switch_rules == None or switch_table == None:
            #FIXME: This shouldn't happen...
            pass
        
        # Save off instructions to local database.
        self._install_rule_in_db(sdx_rule.get_cookie(), of_cookie,
                                 sdx_rule, switch_rules, switch_table)

        # Send instructions to the switch.
        for rule in switch_rules:
            if type(rule) == TranslatedLCRuleContainer:
                self.add_flow(datapath, rule)
            elif type(rule) == TranslatedCorsaRuleContainer:
                self.corsa_rest_cmd(rule)


    def remove_rule(self, datapath, sdx_cookie):
        ''' The main loop calls this to handle removing an existing rule.
            This function removes the existing OpenFlow rules associated with
            a given sdx_cookie. '''

        # Remove a rule.
        # Find the OF cookie based on the SDX Cookie
        of_cookie = self._find_OF_cookie(sdx_cookie)

        # Get the Rules based on the it.
        (swcookie, sdxrule, swrules, table) = self._get_rule_in_db(sdx_cookie)

        try:
            # Remove flows
            for rule in swrules:
                if type(rule) == TranslatedLCRuleContainer:
                    self.remove_flow(datapath, rule)
                elif type(rule) == TranslatedCorsaRuleContainer:
                    # Currently, don't have to do anything here.
                    pass
        except Exception as e:
            self.logger.error("Error in remove_rule")
            self.logger.error("  swcookie: %s" % swcookie)
            self.logger.error("  sdxrule: %s" % sdxrule)
            self.logger.error("  swrules: %s" % swrules)
            self.logger.error("  table: %s" % table)
            raise e

        # Remove rule infomation from database
        self._remove_rule_in_db(sdx_cookie)
                

    def _install_rule_in_db(self, sdxcookie, switchcookie,
                            sdxrule, switchrules, switchtable):
        ''' This installs a rule into the DB. This makes life a lot easier and
            provides a central point to handle DB interactions. '''
        # Columns for the "rules" table:
        #   sdxcookie - The SDX rule's provided cookie. This must be unique.
        #   switchcookie - The generated switch cookie. This will be unique.
        #   sdxrule - The LCRule that the SDX sent down to be installed
        #   switchrules - A list of Ryu-formatted rules that can be sent to the
        #      switch directly
        #   switchtable - The table that the rules are going to be installed.
        #      A single LCRule should only affect one table at a time.

        #FIXME: Checking to make sure it's not already there?
        self.rule_table.insert({'sdxcookie':sdxcookie,
                                'switchcookie':switchcookie,
                                'sdxrule':pickle.dumps(sdxrule),
                                'switchrules':pickle.dumps(switchrules),
                                'switchtable':switchtable})

    def _remove_rule_in_db(self, sdx_cookie):
        ''' This removes a rule from the DB. This makes life a lot easier and
            provides a central point to handle DB interactions. '''
        #FIXME: Make sure it does exist.
        self.rule_table.delete(sdxcookie=sdx_cookie)

    def _get_rule_in_db(self, sdx_cookie):
        ''' This returns a rule from the DB. This makes life a lot easier and 
            provides a central point to handle DB interactions. 
            Returns a tuple:
            (switchcookie, sdxrule, switchrules, switchtable) '''
        result = self.rule_table.find_one(sdxcookie=sdx_cookie)
        if result == None:
            return (None, None, None, None)
        return (result['switchcookie'],
                pickle.loads(str(result['sdxrule'])),
                pickle.loads(str(result['switchrules'])),
                result['switchtable'])
        
        

    def _get_new_OF_cookie(self, sdx_cookie):
        ''' Creates a new cookie that can be used by OpenFlow switches. 
            Populates a local database with information so that cookie can be
            looked up for rule removal. '''
        if self.rule_table.find_one(sdxcookie=sdx_cookie) != None:
            # FIXME: This shouldn't happen...
            pass
        
        of_cookie = self.current_of_cookie
        self.current_of_cookie += 1
        
        return of_cookie

    def _find_OF_cookie(self, sdx_cookie):
        ''' Looks up OpenFlow cookie in local database based on a provided
            sdx_cookie. '''
        result = self.rule_table.find_one(sdxcookie=sdx_cookie)
        if result == None:
            return None
        return result['switchcookie']

    def _find_sdx_cookie(self, of_cookie):
        ''' Loops up the SDX cookie in local database based on a provided
            of_cookie. '''
        result = self.rule_table.find_one(switchcookie=of_cookie)
        if result == None:
            return None
        return result['sdxcookie']

    def _register_packet_in_cb(self, cookie_id, function):
        ''' Used for registeringcookies for packet_in callbacks. Function is 
            called with the packet_in event. '''
        self.logger.warning("Registering cookie 0x%02x to function %s for packet_in handling" % (cookie_id, function))
        self.packet_in_cbs[cookie_id] = function

    def _deregister_packet_in_cb(self, cookie_id):
        ''' Used for deregistering cookies for packet_in callbacks. '''
        del self.packet_in_cbs[cookie_id]

        
    def unknown_source_cb(self, ev):
        ''' Handles new unknown source callbacks. This does two things upon
            receipt of a packet:
              - Sends info to  RyuControllerInterface to eventually send to SDX 
                controller with sdx_cm.send_new_host_port_mapping()
              - Creates new rule to skip forwarding that source address to ctlr
        '''
        # Send info to SDX Controller
        switch_id = 0  # This is unimportant:
                       # it's never used in the translation

        datapath = ev.msg.datapath
        switch_name = self.dpid_data[str(datapath.id)]['name']
        port = ev.msg.match['in_port']
        pkt = packet.Packet(ev.msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        src_address = eth.src

        self.inter_cm_cxn.send_cmd(ICX_UNKNOWN_SOURCE,
                                   {"switch":switch_name,
                                    "port":port,
                                    "src":src_address})

        # New forwarding rule to skip over that again
        matches = [IN_PORT(port), ETH_SRC(src_address)]
        actions = [Continue()]
        table = LEARNINGTABLE
        of_cookie = ev.msg.cookie    # Keep the same cookie as the original rule
        priority = PRIORITY_GENERIC_LEARNED
        marule = MatchActionLCRule(switch_id, matches, actions)
        results = self._translate_MatchActionLCRule(datapath,
                                                    table,
                                                    of_cookie,
                                                    marule,
                                                    priority)
        for rule in results:
            self.add_flow(datapath, rule)
            
    def l2multipoint_unknown_source_cb(self, ev):
        ''' Handles new unknown source callbacks on L2MultipointPolicy edge
            ports. This is very similar to unknown_source_cb(). 
        '''
        # Send info to SDX Controller
        switch_id = 0  # This is unimportant:
                       # it's never used in the translation

        datapath = ev.msg.datapath
        switch_name = self.dpid_data[str(datapath.id)]['name']
        port = ev.msg.match['in_port']
        pkt = packet.Packet(ev.msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        src_address = eth.src
        of_cookie = ev.msg.cookie
        sdx_cookie = self._find_sdx_cookie(of_cookie)
        
        self.inter_cm_cxn.send_cmd(ICX_L2MULTIPOINT_UNKNOWN_SOURCE,
                                   {"cookie":sdx_cookie,
                                    "data": {"dstswitch":switch_name,
                                             "dstport":port,
                                             "dstaddress":src_address}})


        # New forwarding rule to skip over that address in the future.
        matches = [IN_PORT(port), ETH_SRC(src_address)]
        actions = [Continue()]
        table = LEARNINGTABLE
        priority = PRIORITY_L2MULTIPOINT_LEARNED
        marule = MatchActionLCRule(switch_id, matches, actions)
        results = self._translate_MatchActionLCRule(datapath,
                                                    table,
                                                    of_cookie,
                                                    marule,
                                                    priority)
        for rule in results:
            self.add_flow(datapath, rule)
