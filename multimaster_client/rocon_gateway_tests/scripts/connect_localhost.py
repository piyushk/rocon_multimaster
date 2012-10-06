#!/usr/bin/env python
#       
# License: BSD
#   https://raw.github.com/robotics-in-concert/rocon_multimaster/master/multimaster_client/rocon_gateway_tests/LICENSE 
#

import roslib; roslib.load_manifest('rocon_gateway_tests')
import rospy
import gateway_comms.srv
from gateway_comms.srv import ConnectHub
import argparse

"""
  flip_publisher.py script <gateway>
  
  Usage   :
    rosrun rocon_gateway_tests flip_publisher.py
"""

if __name__ == '__main__':

  parser = argparse.ArgumentParser(description='Make a connection to a localhost hub by service call')
#  parser.add_argument("gateway", help="gateway string identifier", type=str)
#  parser.add_argument('-c','--clients',metavar='<Client name>',type=str,nargs='+',help='Client\'s unique name on hub')
#  parser.add_argument('-m','--message',metavar='<Topic triple>',type=str,nargs='+',help='<Topic triple>="<topic name>,<topic type>,<node uri>"')
#  args = parser.parse_args()

  rospy.init_node('connect_localhost')

  connect = rospy.ServiceProxy('/gateway/connect_hub',ConnectHub)
  
  # Form a request message
  req = gateway_comms.srv.ConnectHubRequest() 
  req.uri = "http://localhost:6379"
  print ""
  print "== Request =="
  print ""
  print req
  print ""
  resp = connect(req)
  print "== Response =="
  print ""
  print resp

