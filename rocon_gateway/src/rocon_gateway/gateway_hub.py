#
# License: BSD
#
#   https://raw.github.com/robotics-in-concert/rocon_multimaster/license/LICENSE
#
###############################################################################
# Imports
###############################################################################

import threading
import rospy
import re
import utils
from gateway_msgs.msg import RemoteRule as FlipStatus
import gateway_msgs.msg as gateway_msgs
import rocon_python_comms
import rocon_python_utils
import rocon_gateway_utils
import rocon_hub_client
import rocon_python_redis as redis
import time
from rocon_hub_client import hub_api, hub_client
from rocon_hub_client.exceptions import HubConnectionLostError, \
    HubNameNotFoundError, HubNotFoundError

from .exceptions import GatewayUnavailableError

###############################################################################
# Redis Connection Checker
##############################################################################


class HubConnectionCheckerThread(threading.Thread):

    '''
      Pings redis periodically to figure out if redis is still alive.
    '''

    def __init__(self, ip, port, hub_connection_lost_hook):
        threading.Thread.__init__(self)
        self.daemon = True  # clean shut down of thread when hub connection is lost
        self.ping_frequency = 0.2  # Too spammy? # TODO Need to parametrize
        self._hub_connection_lost_hook = hub_connection_lost_hook
        self.ip = ip
        self.port = port
        self.pinger = rocon_python_utils.network.Pinger(self.ip, self.ping_frequency)

    def get_latency(self):
        return self.pinger.get_latency()

    def run(self):
        # This runs in the background to gather the latest connection statistics
        # Note - it's not used in the keep alive check
        self.pinger.start()
        rate = rocon_python_comms.WallRate(self.ping_frequency)
        alive = True
        while alive:
            alive = hub_client.ping_hub(self.ip, self.port)
            rate.sleep()
        self._hub_connection_lost_hook()

##############################################################################
# Hub
##############################################################################


class GatewayHub(rocon_hub_client.Hub):

    def __init__(self, ip, port, whitelist, blacklist):
        '''
          @param remote_gateway_request_callbacks : to handle redis responses
          @type list of function pointers (back to GatewaySync class

          @param ip : redis server ip
          @param port : redis server port

          @raise HubNameNotFoundError, HubNotFoundError
        '''
        try:
            super(GatewayHub, self).__init__(ip, port, whitelist, blacklist)  # can just do super() in python3
        except HubNotFoundError:
            raise
        except HubNameNotFoundError:
            raise
        self._hub_connection_lost_gateway_hook = None
        self._firewall = 0

        # Setting up some basic parameters in-case we use this API without registering a gateway
        self._redis_keys['gatewaylist'] = hub_api.create_rocon_hub_key('gatewaylist')
        self._unique_gateway_name = ''

    ##########################################################################
    # Hub Connections
    ##########################################################################

    def register_gateway(self, firewall, unique_gateway_name, hub_connection_lost_gateway_hook, gateway_ip):
        '''
          Register a gateway with the hub.

          @param firewall
          @param unique_gateway_name
          @param hub_connection_lost_gateway_hook : used to trigger Gateway.disengage_hub(hub)
                 on lost hub connections in redis pubsub listener thread.
          @gateway_ip

          @raise HubConnectionLostError if for some reason, the redis server has become unavailable.
        '''
        if not self._redis_server:
            raise HubConnectionLostError()
        self._unique_gateway_name = unique_gateway_name
        self._redis_keys['gateway'] = hub_api.create_rocon_key(unique_gateway_name)
        self._redis_keys['firewall'] = hub_api.create_rocon_gateway_key(unique_gateway_name, 'firewall')
        self._firewall = 1 if firewall else 0
        self._hub_connection_lost_gateway_hook = hub_connection_lost_gateway_hook
        if not self._redis_server.sadd(self._redis_keys['gatewaylist'], self._redis_keys['gateway']):
            # should never get here - unique should be unique
            pass
        self.mark_named_gateway_available(self._redis_keys['gateway'])
        self._redis_server.set(self._redis_keys['firewall'], self._firewall)
        # I think we just used this for debugging, but we might want to hide it in
        # future (it's the ros master hostname/ip)
        self._redis_keys['ip'] = hub_api.create_rocon_gateway_key(unique_gateway_name, 'ip')
        self._redis_server.set(self._redis_keys['ip'], gateway_ip)

        self.private_key, public_key = utils.generate_private_public_key()
        self._redis_keys['public_key'] = hub_api.create_rocon_gateway_key(unique_gateway_name, 'public_key')
        self._redis_server.set(self._redis_keys['public_key'], utils.serialize_key(public_key))

        # Mark this gateway as now available
        self._redis_server.sadd(self._redis_keys['gatewaylist'], self._redis_keys['gateway'])
        self.hub_connection_checker_thread = HubConnectionCheckerThread(
            self.ip, self.port, self._hub_connection_lost_hook)
        self.hub_connection_checker_thread.start()
        self.connection_lost_lock = threading.Lock()

        # Let hub know we are alive
        ping_key = hub_api.create_rocon_gateway_key(self._unique_gateway_name, ':ping')
        self._redis_server.set(ping_key, True)
        self._redis_server.expire(ping_key, gateway_msgs.ConnectionStatistics.MAX_TTL)

    def _hub_connection_lost_hook(self):
        '''
          This gets triggered by the redis connection checker thread when the hub connection is lost.
          The trigger is passed to the gateway who needs to remove the hub.
        '''
        self.connection_lost_lock.acquire()
        # should probably have a try: except AttributeError here as the following is not atomic.
        if self._hub_connection_lost_gateway_hook is not None:
            rospy.loginfo("Gateway : Lost connection with hub. Attempting to disconnect...")
            self._hub_connection_lost_gateway_hook(self)
            self._hub_connection_lost_gateway_hook = None
        self.connection_lost_lock.release()

    def unregister_gateway(self):
        '''
          Remove all gateway info from the hub.

          @return: success or failure of the operation
          @rtype: bool
        '''
        try:
            self.unregister_named_gateway(self._redis_keys['gateway'])
        except (redis.exceptions.ConnectionError, redis.exceptions.ResponseError):
            # usually just means the hub has gone down just before us or is in the
            # middle of doing so let it die nice and peacefully
            # rospy.logwarn("Gateway : problem unregistering from the hub " +
            #               "(likely that hub shutdown before the gateway).")
            pass
        # should we not also shut down self.remote_gatew
        rospy.loginfo("Gateway : unregistered from the hub [%s]" % self.name)

    def publish_network_statistics(self, statistics):
        '''
          Publish network interface information to the hub

          @param statistics
          @type gateway_msgs.RemoteGateway
        '''
        try:
            network_info_available = hub_api.create_rocon_gateway_key(
                self._unique_gateway_name, 'network:info_available')
            self._redis_server.set(network_info_available, statistics.network_info_available)
            if not statistics.network_info_available:
                return
            network_type = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'network:type')
            self._redis_server.set(network_type, statistics.network_type)
            # Let hub know that we are alive - even for wired connections. Perhaps something can
            # go wrong for them too, though no idea what. Anyway, writing one entry is low cost
            # and it makes the logic easier on the hub side.
            ping_key = hub_api.create_rocon_gateway_key(self._unique_gateway_name, ':ping')
            self._redis_server.set(ping_key, True)
            self._redis_server.expire(ping_key, gateway_msgs.ConnectionStatistics.MAX_TTL)
            # Update latency statistics
            latency = self.hub_connection_checker_thread.get_latency()
            self.update_named_gateway_latency_stats(self._unique_gateway_name, latency)
            # If wired, don't worry about wireless statistics.
            if statistics.network_type == gateway_msgs.RemoteGateway.WIRED:
                return
            wireless_bitrate_key = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'wireless:bitrate')
            self._redis_server.set(wireless_bitrate_key, statistics.wireless_bitrate)
            wireless_link_quality = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'wireless:quality')
            self._redis_server.set(wireless_link_quality, statistics.wireless_link_quality)
            wireless_signal_level = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'wireless:signal_level')
            self._redis_server.set(wireless_signal_level, statistics.wireless_signal_level)
            wireless_noise_level = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'wireless:noise_level')
            self._redis_server.set(wireless_noise_level, statistics.wireless_noise_level)
        except (redis.exceptions.ConnectionError, redis.exceptions.ResponseError):
            rospy.logerr("Gateway: Unable to update network interface information")

    def unregister_named_gateway(self, gateway_key):
        '''
          Remove all gateway info for given gateway key from the hub.
        '''
        try:
            gateway_keys = self._redis_server.keys(gateway_key + ":*")
            pipe = self._redis_server.pipeline()
            pipe.delete(*gateway_keys)
            pipe.srem(self._redis_keys['gatewaylist'], gateway_key)
            pipe.execute()
        except (redis.exceptions.ConnectionError, redis.exceptions.ResponseError):
            pass

    def update_named_gateway_latency_stats(self, gateway_name, latency_stats):
        '''
          For a given gateway, update the latency statistics

          #param gateway_name : gateway name, not the redis key
          @type str
          @param latency_stats : ping statistics to the gateway from the hub
          @type list : 4-tuple of float values [min, avg, max, mean deviation]
        '''
        try:
            min_latency_key = hub_api.create_rocon_gateway_key(gateway_name, 'latency:min')
            avg_latency_key = hub_api.create_rocon_gateway_key(gateway_name, 'latency:avg')
            max_latency_key = hub_api.create_rocon_gateway_key(gateway_name, 'latency:max')
            mdev_latency_key = hub_api.create_rocon_gateway_key(gateway_name, 'latency:mdev')
            self._redis_server.set(min_latency_key, latency_stats[0])
            self._redis_server.set(avg_latency_key, latency_stats[1])
            self._redis_server.set(max_latency_key, latency_stats[2])
            self._redis_server.set(mdev_latency_key, latency_stats[3])
        except (redis.exceptions.ConnectionError, redis.exceptions.ResponseError):
            rospy.logerr("Gateway: unable to update latency stats for " + gateway_name)

    def mark_named_gateway_available(self, gateway_key, available=True,
                                     time_since_last_seen=0.0):
        '''
          This function is used by the hub to mark if a gateway can be pinged.
          If a gateway cannot be pinged, the hub indicates how longs has it been
          since the hub was last seen

          @param gateway_key : The gateway key (not the name)
          @type str
          @param available: If the gateway can be pinged right now
          @type bool
          @param time_since_last_seen: If available is false, how long has it
                 been since the gateway was last seen (in seconds)
          @type float
        '''
        available_key = gateway_key + ":available"
        self._redis_server.set(available_key, available)
        time_since_last_seen_key = gateway_key + ":time_since_last_seen"
        self._redis_server.set(time_since_last_seen_key, int(time_since_last_seen))

    ##########################################################################
    # Hub Data Retrieval
    ##########################################################################

    def remote_gateway_info(self, gateway):
        '''
          Return remote gateway information for the specified gateway string id.

          @param gateways : gateway id string to search for
          @type string
          @return remote gateway information
          @rtype gateway_msgs.RemotGateway or None
        '''
        firewall = self._redis_server.get(hub_api.create_rocon_gateway_key(gateway, 'firewall'))
        if firewall is None:
            return None  # equivalent to saying no gateway of this id found
        ip = self._redis_server.get(hub_api.create_rocon_gateway_key(gateway, 'ip'))
        if ip is None:
            return None  # hub information not available/correct
        remote_gateway = gateway_msgs.RemoteGateway()
        remote_gateway.name = gateway
        remote_gateway.ip = ip
        remote_gateway.firewall = True if int(firewall) else False
        remote_gateway.public_interface = []
        encoded_advertisements = self._redis_server.smembers(
            hub_api.create_rocon_gateway_key(gateway, 'advertisements'))
        for encoded_advertisement in encoded_advertisements:
            advertisement = utils.deserialize_connection(encoded_advertisement)
            remote_gateway.public_interface.append(advertisement.rule)
        remote_gateway.flipped_interface = []
        encoded_flips = self._redis_server.smembers(hub_api.create_rocon_gateway_key(gateway, 'flips'))
        for encoded_flip in encoded_flips:
            [target_gateway, name, connection_type, node] = utils.deserialize(encoded_flip)
            remote_rule = gateway_msgs.RemoteRule(target_gateway, gateway_msgs.Rule(connection_type, name, node))
            remote_gateway.flipped_interface.append(remote_rule)
        remote_gateway.pulled_interface = []
        encoded_pulls = self._redis_server.smembers(hub_api.create_rocon_gateway_key(gateway, 'pulls'))
        for encoded_pull in encoded_pulls:
            [target_gateway, name, connection_type, node] = utils.deserialize(encoded_pull)
            remote_rule = gateway_msgs.RemoteRule(target_gateway, gateway_msgs.Rule(connection_type, name, node))
            remote_gateway.pulled_interface.append(remote_rule)

        # Gateway health/network connection statistics indicators
        gateway_available_key = hub_api.create_rocon_gateway_key(gateway, 'available')
        remote_gateway.conn_stats.gateway_available = \
            self._parse_redis_bool(self._redis_server.get(gateway_available_key))
        time_since_last_seen_key = hub_api.create_rocon_gateway_key(gateway, 'time_since_last_seen')
        remote_gateway.conn_stats.time_since_last_seen = \
            self._parse_redis_int(self._redis_server.get(time_since_last_seen_key))

        ping_latency_min_key = hub_api.create_rocon_gateway_key(gateway, 'latency:min')
        remote_gateway.conn_stats.ping_latency_min = \
            self._parse_redis_float(self._redis_server.get(ping_latency_min_key))
        ping_latency_max_key = hub_api.create_rocon_gateway_key(gateway, 'latency:max')
        remote_gateway.conn_stats.ping_latency_max = \
            self._parse_redis_float(self._redis_server.get(ping_latency_max_key))
        ping_latency_avg_key = hub_api.create_rocon_gateway_key(gateway, 'latency:avg')
        remote_gateway.conn_stats.ping_latency_avg = \
            self._parse_redis_float(self._redis_server.get(ping_latency_avg_key))
        ping_latency_mdev_key = hub_api.create_rocon_gateway_key(gateway, 'latency:mdev')
        remote_gateway.conn_stats.ping_latency_mdev = \
            self._parse_redis_float(self._redis_server.get(ping_latency_mdev_key))

        # Gateway network connection indicators
        network_info_available_key = hub_api.create_rocon_gateway_key(gateway, 'network:info_available')
        remote_gateway.conn_stats.network_info_available = \
            self._parse_redis_bool(self._redis_server.get(network_info_available_key))
        if not remote_gateway.conn_stats.network_info_available:
            return remote_gateway
        network_type_key = hub_api.create_rocon_gateway_key(gateway, 'network:type')
        remote_gateway.conn_stats.network_type = \
            self._parse_redis_int(self._redis_server.get(network_type_key))
        if remote_gateway.conn_stats.network_type == gateway_msgs.RemoteGateway.WIRED:
            return remote_gateway
        wireless_bitrate_key = hub_api.create_rocon_gateway_key(gateway, 'wireless:bitrate')
        remote_gateway.conn_stats.wireless_bitrate = \
            self._parse_redis_float(self._redis_server.get(wireless_bitrate_key))
        wireless_link_quality_key = hub_api.create_rocon_gateway_key(gateway, 'wireless:quality')
        remote_gateway.conn_stats.wireless_link_quality = \
            self._parse_redis_int(self._redis_server.get(wireless_link_quality_key))
        wireless_signal_level_key = hub_api.create_rocon_gateway_key(gateway, 'wireless:signal_level')
        remote_gateway.conn_stats.wireless_signal_level = \
            self._parse_redis_float(self._redis_server.get(wireless_signal_level_key))
        wireless_noise_level_key = hub_api.create_rocon_gateway_key(gateway, 'wireless:noise_level')
        remote_gateway.conn_stats.wireless_noise_level = \
            self._parse_redis_float(self._redis_server.get(wireless_noise_level_key))
        return remote_gateway

    def list_remote_gateway_names(self):
        '''
          Return a list of the gateways (name list, not redis keys).
          e.g. ['gateway32adcda32','pirate21fasdf']. If not connected, just
          returns an empty list.
        '''
        if not self._redis_server:
            rospy.logerr("Gateway : cannot retrieve remote gateway names [%s][%s]." % (self.name, self.uri))
            return []
        gateways = []
        try:
            gateway_keys = self._redis_server.smembers(self._redis_keys['gatewaylist'])
            for gateway in gateway_keys:
                if hub_api.key_base_name(gateway) != self._unique_gateway_name:
                    gateways.append(hub_api.key_base_name(gateway))
        except (redis.ConnectionError, AttributeError) as unused_e:
            # redis misbehaves a little here, sometimes it doesn't catch a disconnection properly
            # see https://github.com/robotics-in-concert/rocon_multimaster/issues/251 so it
            # pops up as an AttributeError
            pass
        return gateways

    def matches_remote_gateway_name(self, gateway):
        '''
          Use this when gateway can be a regular expression and
          we need to check it off against list_remote_gateway_names()

          @return a list of matches (higher level decides on action for duplicates).
          @rtype list[str] : list of remote gateway names.
        '''
        matches = []
        try:
            for remote_gateway in self.list_remote_gateway_names():
                if re.match(gateway, remote_gateway):
                    matches.append(remote_gateway)
        except HubConnectionLostError:
            raise
        return matches

    def matches_remote_gateway_basename(self, gateway):
        '''
          Use this when gateway can be a regular expression and
          we need to check it off against list_remote_gateway_names()
        '''
        weak_matches = []
        try:
            for remote_gateway in self.list_remote_gateway_names():
                if re.match(gateway, rocon_gateway_utils.gateway_basename(remote_gateway)):
                    weak_matches.append(remote_gateway)
        except HubConnectionLostError:
            raise
        return weak_matches

    def get_remote_connection_state(self, remote_gateway):
        '''
          Equivalent to get_connection_state, but generates it from the public
          interface of a remote gateway

          @param remote_gateway : hash name for a remote gateway
          @type str
          @return dictionary of remote advertisements
          @rtype dictionary of connection type keyed connection values
       '''
        connections = utils.create_empty_connection_type_dictionary()
        key = hub_api.create_rocon_gateway_key(remote_gateway, 'advertisements')
        try:
            public_interface = self._redis_server.smembers(key)
            for connection_str in public_interface:
                connection = utils.deserialize_connection(connection_str)
                connections[connection.rule.type].append(connection)
        except redis.exceptions.ConnectionError:
            # will arrive here if the hub happens to have been lost last update and arriving here
            pass
        return connections

    def get_remote_gateway_firewall_flag(self, gateway):
        '''
          Returns the value of the remote gateway's firewall (flip)
          flag.

          @param gateway : gateway string id
          @param string

          @return state of the flag
          @rtype Bool

          @raise GatewayUnavailableError when specified gateway is not on the hub
        '''
        firewall = self._redis_server.get(hub_api.create_rocon_gateway_key(gateway, 'firewall'))
        if firewall is not None:
            return True if int(firewall) else False
        else:
            raise GatewayUnavailableError

    def get_local_advertisements(self):
        '''
          Retrieves the local list of advertisements from the hub. This
          gets used to sync across multiple hubs.

          @return dictionary of remote advertisements
          @rtype dictionary of connection type keyed connection values
       '''
        connections = utils.create_empty_connection_type_dictionary()
        key = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'advertisements')
        public_interface = self._redis_server.smembers(key)
        for connection_str in public_interface:
            connection = utils.deserialize_connection(connection_str)
            connections[connection.rule.type].append(connection)
        return connections

    def _parse_redis_float(self, val):
        if val:
            return float(val)
        else:
            return 0.0

    def _parse_redis_int(self, val):
        if val:
            return int(val)
        else:
            return 0.0

    def _parse_redis_bool(self, val):
        if val and (val == 'True' or val):
            return True
        else:
            return False

    ##########################################################################
    # Posting Information to the Hub
    ##########################################################################

    def advertise(self, connection):
        '''
          Places a topic, service or action on the public interface. On the
          redis server, this representation will always be:

           - topic : a triple { name, type, xmlrpc node uri }
           - service : a triple { name, rosrpc uri, xmlrpc node uri }
           - action : ???

          @param connection: representation of a connection (topic, service, action)
          @type  connection: str
          @raise .exceptions.ConnectionTypeError: if connection arg is invalid.
        '''
        key = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'advertisements')
        msg_str = utils.serialize_connection(connection)
        self._redis_server.sadd(key, msg_str)

    def unadvertise(self, connection):
        '''
          Removes a topic, service or action from the public interface.

          @param connection: representation of a connection (topic, service, action)
          @type  connection: str
          @raise .exceptions.ConnectionTypeError: if connectionarg is invalid.
        '''
        key = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'advertisements')
        msg_str = utils.serialize_connection(connection)
        self._redis_server.srem(key, msg_str)

    def post_flip_details(self, gateway, name, connection_type, node):
        '''
          Post flip details to the redis server. This has no actual functionality,
          it is just useful for debugging with the remote_gateway_info service.

          @param gateway : the target of the flip
          @type string
          @param name : the name of the connection
          @type string
          @param type : the type of the connection (one of ConnectionType.xxx
          @type string
          @param node : the node name it was pulled from
          @type string
        '''
        key = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'flips')
        serialized_data = utils.serialize([gateway, name, connection_type, node])
        self._redis_server.sadd(key, serialized_data)

    def remove_flip_details(self, gateway, name, connection_type, node):
        '''
          Post flip details to the redis server. This has no actual functionality,
          it is just useful for debugging with the remote_gateway_info service.

          @param gateway : the target of the flip
          @type string
          @param name : the name of the connection
          @type string
          @param type : the type of the connection (one of ConnectionType.xxx
          @type string
          @param node : the node name it was pulled from
          @type string
        '''
        key = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'flips')
        serialized_data = utils.serialize([gateway, name, connection_type, node])
        self._redis_server.srem(key, serialized_data)

    def post_pull_details(self, gateway, name, connection_type, node):
        '''
          Post pull details to the hub. This has no actual functionality,
          it is just useful for debugging with the remote_gateway_info service.

          @param gateway : the gateway it is pulling from
          @type string
          @param name : the name of the connection
          @type string
          @param type : the type of the connection (one of ConnectionType.xxx
          @type string
          @param node : the node name it was pulled from
          @type string
        '''
        key = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'pulls')
        serialized_data = utils.serialize([gateway, name, connection_type, node])
        self._redis_server.sadd(key, serialized_data)

    def remove_pull_details(self, gateway, name, connection_type, node):
        '''
          Post pull details to the hub. This has no actual functionality,
          it is just useful for debugging with the remote_gateway_info service.

          @param gateway : the gateway it was pulling from
          @type string
          @param name : the name of the connection
          @type string
          @param type : the type of the connection (one of ConnectionType.xxx
          @type string
          @param node : the node name it was pulled from
          @type string
        '''
        key = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'pulls')
        serialized_data = utils.serialize([gateway, name, connection_type, node])
        self._redis_server.srem(key, serialized_data)

    ##########################################################################
    # Flip specific communication
    ##########################################################################

    def get_unblocked_flipped_in_connections(self):
        '''
          Returns all unblocked flips (accepted or pending) that have been
          requested through this hub
        '''
        registrations = []
        key = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'flip_ins')
        encoded_flip_ins = []
        try:
            encoded_flip_ins = self._redis_server.smembers(key)
        except (redis.ConnectionError, AttributeError) as unused_e:
            # probably disconnected from the hub
            pass
        for flip_in in encoded_flip_ins:
            cmd, source, connection_list = utils.deserialize_request(flip_in)
            connection = utils.get_connection_from_list(connection_list)
            connection = utils.decrypt_connection(connection, self.private_key)
            if cmd != FlipStatus.BLOCKED:
                registrations.append(utils.Registration(connection, source))
        return registrations

    def block_flip_request(self, registration):
        ''' Convenience wrapper for updating flip request status '''
        return self._update_flip_request_status(registration, FlipStatus.BLOCKED)

    def accept_flip_request(self, registration):
        ''' Convenience wrapper for updating flip request status '''
        return self._update_flip_request_status(registration, FlipStatus.ACCEPTED)

    def _update_flip_request_status(self, registration, status):
        '''
          Updates the flip request status for this hub

          @param registration : the flip registration for which we are updating status
          @type utils.Registration

          @param status : pending/accepted/blocked
          @type same as gateway_msgs.msg.RemoteRule.status

          @return True if this hub was used to send the flip request, and the status was updated. False otherwise.
          @rtype Boolean
        '''
        hub_found = False
        key = hub_api.create_rocon_gateway_key(self._unique_gateway_name, 'flip_ins')
        encoded_flip_ins = self._redis_server.smembers(key)
        for flip_in in encoded_flip_ins:
            cmd, source, connection_list = utils.deserialize_request(flip_in)
            connection = utils.get_connection_from_list(connection_list)
            connection = utils.decrypt_connection(connection, self.private_key)
            if source == registration.remote_gateway and \
               connection == registration.connection:
                self._redis_server.srem(key, flip_in)
                hub_found = True
        if hub_found:
            encrypted_connection = utils.encrypt_connection(registration.connection,
                                                            self.private_key)
            serialized_data = utils.serialize_connection_request(status,
                                                                 registration.remote_gateway,
                                                                 encrypted_connection)
            self._redis_server.sadd(key, serialized_data)
            return True
        return False

    def get_flip_request_status(self, remote_gateway, rule, source_gateway=None):
        '''
          Get the status of a flipped registration. If the flip request does not
          exist (for instance, in the case where this hub was not used to send
          the request), then None is returned

          @return the flip status or None
          @rtype same as gateway_msgs.msg.RemoteRule.status or None
        '''
        if source_gateway is None:
            source_gateway = self._unique_gateway_name
        key = hub_api.create_rocon_gateway_key(remote_gateway, 'flip_ins')
        encoded_flips = self._redis_server.smembers(key)
        for flip in encoded_flips:
            cmd, source, connection_list = utils.deserialize_request(flip)
            if source != source_gateway:
                continue
            connection = utils.get_connection_from_list(connection_list)
            # Compare rules as xmlrpc_uri and type_info are encrypted
            if connection.rule == rule:
                return cmd
        # Probably, this hub was not used to send this flip request
        return None

    def send_flip_request(self, remote_gateway, connection, timeout=15.0):
        '''
          Sends a message to the remote gateway via redis pubsub channel. This is called from the
          watcher thread, when a flip rule gets activated.

           - redis channel name: rocon:<remote_gateway_name>
           - data : list of [ command, gateway, rule type, type, xmlrpc_uri ]
            - [0] - command       : in this case 'flip'
            - [1] - gateway       : the name of this gateway, i.e. the flipper
            - [2] - name          : local name
            - [3] - node          : local node name
            - [4] - connection_type : one of ConnectionType.PUBLISHER etc
            - [5] - type_info     : a ros format type (e.g. std_msgs/String or service api)
            - [6] - xmlrpc_uri    : the xmlrpc node uri

          @param command : string command name - either 'flip' or 'unflip'
          @type str

          @param flip_rule : the flip to send
          @type gateway_msgs.RemoteRule

          @param type_info : topic type (e.g. std_msgs/String)
          @param str

          @param xmlrpc_uri : the node uri
          @param str
        '''
        key = hub_api.create_rocon_gateway_key(remote_gateway, 'flip_ins')
        source = hub_api.key_base_name(self._redis_keys['gateway'])

        # Encrypt the transmission
        start_time = time.time()
        while time.time() - start_time <= timeout:
            remote_gateway_public_key_str = self._redis_server.get(
                hub_api.create_rocon_gateway_key(remote_gateway, 'public_key'))
            if remote_gateway_public_key_str is not None:
                break
        if remote_gateway_public_key_str is None:
            rospy.logerr("Gateway : flip to " + remote_gateway +
                         " failed as public key not found")
            return False

        remote_gateway_public_key = utils.deserialize_key(remote_gateway_public_key_str)
        encrypted_connection = utils.encrypt_connection(connection, remote_gateway_public_key)

        # Send data
        serialized_data = utils.serialize_connection_request(
            FlipStatus.PENDING, source, encrypted_connection)
        self._redis_server.sadd(key, serialized_data)
        return True

    def send_unflip_request(self, remote_gateway, rule):
        if rule.type == gateway_msgs.ConnectionType.ACTION_CLIENT:
            action_name = rule.name
            rule.type = gateway_msgs.ConnectionType.PUBLISHER
            rule.name = action_name + "/goal"
            self._send_unflip_request(remote_gateway, rule)
            rule.name = action_name + "/cancel"
            self._send_unflip_request(remote_gateway, rule)
            rule.type = gateway_msgs.ConnectionType.SUBSCRIBER
            rule.name = action_name + "/feedback"
            self._send_unflip_request(remote_gateway, rule)
            rule.name = action_name + "/status"
            self._send_unflip_request(remote_gateway, rule)
            rule.name = action_name + "/result"
            self._send_unflip_request(remote_gateway, rule)
        elif rule.type == gateway_msgs.ConnectionType.ACTION_SERVER:
            action_name = rule.name
            rule.type = gateway_msgs.ConnectionType.SUBSCRIBER
            rule.name = action_name + "/goal"
            self._send_unflip_request(remote_gateway, rule)
            rule.name = action_name + "/cancel"
            self._send_unflip_request(remote_gateway, rule)
            rule.type = gateway_msgs.ConnectionType.PUBLISHER
            rule.name = action_name + "/feedback"
            self._send_unflip_request(remote_gateway, rule)
            rule.name = action_name + "/status"
            self._send_unflip_request(remote_gateway, rule)
            rule.name = action_name + "/result"
            self._send_unflip_request(remote_gateway, rule)
        else:
            self._send_unflip_request(remote_gateway, rule)

    def _send_unflip_request(self, remote_gateway, rule):
        '''
          Unflip a previously flipped registration. If the flip request does not
          exist (for instance, in the case where this hub was not used to send
          the request), then False is returned

          @return True if the flip existed and was removed, False otherwise
          @rtype Boolean
        '''
        key = hub_api.create_rocon_gateway_key(remote_gateway, 'flip_ins')
        encoded_flip_ins = self._redis_server.smembers(key)
        for flip_in in encoded_flip_ins:
            cmd, source, connection_list = utils.deserialize_request(flip_in)
            connection = utils.get_connection_from_list(connection_list)
            if source == hub_api.key_base_name(self._redis_keys['gateway']) and \
               rule == connection.rule:
                self._redis_server.srem(key, flip_in)
                return True
        return False
