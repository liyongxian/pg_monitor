#!/usr/bin/env python

from __future__ import print_function

"""
Script to monitor the master and slave nodes and automate the promotion of the slave should the master fail.
"""

import os
import os.path
import imp
from sqlalchemy import *
import sqlalchemy.exc
import time
import datetime
import subprocess
import logging

import argparse
import pid

from alerts import EmailAlertManager

mylog = None

def getHostname():
    import os
    return os.uname()[1].split(".")[0]

def import_config( name, filepath):
  d = imp.new_module( name )
  d.__file__ = filepath

  cfgmap = {}
  try:
    with open(filepath) as cfg_file:
      exec( compile( cfg_file.read(), filepath, 'exec'), cfgmap )
  except IOError as e:
    e.strerror = 'Unable to load config file (%s)' % e.strerror
    raise
  return cfgmap

def isempty(str):
    return not( str and str.strip() != "" )

class MasterFailed(Exception):
    pass

class ValidateRepmgr(object):

    def validate(self, repmgr):
        """Check for the values we need to successfully monitor the cluster"""

        assert repmgr.get( 'promote_command' )
        assert repmgr.get( 'reconnect_attempts' )
        assert repmgr.get( 'reconnect_interval' )
        assert repmgr.get( 'cluster' )
        assert repmgr.get( 'node' )
        assert repmgr.get( 'node_name' )
        assert repmgr.get( 'failover' )
        assert repmgr.get( 'master_response_timeout' )        
        
        

class PgClusterMonitor(object):

    def __init__(self, configfile, quiet=False):
        self.configfile = configfile
        self.config = self.loadConfig( configfile )
        self.quiet = quiet
      
        self.pgnodes = self.config['PG_NODES']

        self.master = None
        for node in self.pgnodes:
            name = node.get( "name" , None )
            if name:
                node[ 'conninfo' ]  = self.config['PG_CONN_INFO'].get( name, None )
            if node[ "type" ] == "master":
                self.master = name

        self.loadReprMgr( self.config[ 'REPMGR_CONFIG' ] )

        self.cluster = self.repmgr[ 'cluster' ]
        self.failover = self.repmgr.get( 'failover', 'manual' )
        self.promote_command = self.repmgr[ 'promote_command' ]
        self.reconnect_attempts = self.repmgr[ 'reconnect_attempts' ]
        self.reconnect_interval = self.repmgr[ 'reconnect_interval' ]

        self.alertman = None
        if self.config.get('ALERT_NOTIFY_LIST', None):
            self.alertman = EmailAlertManager( self.config['ALERT_SMTPHOST'] , self.config['ALERT_NOTIFY_LIST'], self.config['ALERT_FROM_EMAIL' ] )
        else:
            log.warn( "[Warning] No alert notifications are configured." )
        self.failedAt = None
        self.lastPromotion = None
        self.recovered = False
        self.lastAliveLog = None
        self.promote_mode = 'manual'

    def alert(self, subject, msg):
      notifyinterval = 120
      limit = 100
      if self.alertman:
        while limit>0:
          mylog.info( "Sending alert '%s' emails to %s" % (subject, self.alertman.addresslist ) )
          self.alertman.alert( subject, msg)
          time.sleep(notifyinterval)
          notifyinterval=notifyinterval*2
          limit=limit-1
          if self.recovered:
            # On recovery one e-mail should be enough.
            limit=0

    def loadConfig(self, configfile ):
      try:
        cfg = import_config( "config", configfile )
        assert cfg['PG_NODES']
        assert cfg['PG_CONN_INFO']
        assert cfg['REPMGR_CONFIG']
      except:
        mylog.error( "configfile speciied is invalid. See standard out for details." )
        print( """configfile must be specified and reference a valid configuration file. 
A valid configuration is a python file (with any extension) with the following parameters set:

# An enumeration of all nodes in the cluster
PG_NODES = [ { 'name': 'pgnode1', 'type': 'master' }, {'name': 'pgnode2', 'type': 'slave'} ]
# A dictionary of connect strings for every node in the cluster
PG_CONN_INFO = { 'pgnode1' : 'postgresql://user:password@pgnode1.host.com:5432/dbname' }
# The location of repmgr.conf file for the node. This is the same file you pass to repmgr commands
REPR_CONFIG = "/var/lib/postgresql/repmgr/repmgr.conf"

""" )
        raise

      return cfg
        
    def loadReprMgr(self, repmgrconf):
        self.repmgr = import_config( "repmgr", repmgrconf )
        v = ValidateRepmgr()
        v.validate( self.repmgr )

    def logalive(self):
        """On a periodic basis, log that the monitor is active."""

        now =  datetime.datetime.now()
        if not(self.lastAliveLog) or (now - self.lastAliveLog).seconds > (3600) :
            mylog.info( "Alive and monitoring cluster %s." % self.cluster )
            self.lastAliveLog = now

        
    def monitor(self):

        # Monitor until a failure event occurs.
        failure=False

        try:
            while not( failure ):
                try:
                    enginemap = self.check_nodes()

                    if not(self.quiet):
                      print("[%s] Cluster %s is healthy." % (datetime.datetime.now(), self.cluster) )
                    time.sleep(15)
                    self.logalive()
                except MasterFailed as mf:
                    failure = True
                    clusterState= "degraded" if self.recovered else "failed"

                    msg = ""
                    if clusterState == "failed":
                      msg = "\n\nQuick action is required to restore the cluster to health! Please bring the cluster back up."
                    else:
                      msg = "\n\nThe master node must be restored to bring the cluster to a healthy state."

                    self.alert( 'Master Failed',
                                """Cluster %s is currently in a %s state. %s

Monitor on %s observed Master %s fail at %s. 

Output from the last attempt to promote it's slave:

%s\n

Please Check on the health of the cluster.

To stop receiving these notifications. Login it to server %s and 
execute the following command:
    sudo /etc/init.d/pgmonitor stop
"""
                            % ( self.cluster, clusterState, msg, getHostname(), self.master, self.failedAt, self.fixnewlines(self.lastPromotion).strip(), getHostname()   )
                    )
                    raise
        finally:
            self.alert( "pg_monitor exiting, final message",
                        """pg_monitor is exiting. Please investigate the %s cluster failure. 

Please take corrective action to restore the cluster. 

Afterwards restart pg_monitor to resume monitoring of the clustering for a failover event.
  /etc/init.d/pgmonitor restart

Thank you,
pg_monitor@%s
""" % (self.cluster, getHostname() ) )

    def fixnewlines(self, str):
      return str.replace('\\r',chr(13)).replace('\\n',chr(10))

    def check_nodes(self):
        enginemap = {}
        for n in self.pgnodes:
            conn = self.check_node(n)
            enginemap[ n.get('name')] = n

            if not(conn) and n.get('type') == 'master':
                if not(self.failedAt):
                  self.failedAt = datetime.datetime.now()
                print( "[%s] master failed" % datetime.datetime.now() )
                mylog.warn( "Master failed" )
                self.promote_slave()
                
                raise MasterFailed( "Master failed" )

            conn.close()

        return enginemap

    def _promote_slave(self):
        mylog.warn( "Promoting slave to be cluster active master" )
        mylog.info( self.promote_command )
        print( "%s" % self.promote_command )
        proc = subprocess.Popen( self.promote_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT )

        pstdout = proc.communicate()[0]
        self.lastPromotion = "\n".join( [ repr(pstdout)  ] )

        print( repr(pstdout) )

        if "STANDBY PROMOTE successful" in pstdout:
            mylog.info( "Slave promotion was succesful" )
        else:
            mylog.warn( "Slave promotion to cluster active master failed!" )
            mylog.error( repr(pstdout) )
        return proc.returncode == 0

        

    def promote_slave(self):
      reconnect_attempts = self.reconnect_attempts
      success = False

      conn = None
      for n in self.pgnodes:
        if n['type'] == 'slave':
          conn = self.check_node( n )
      

      while conn and reconnect_attempts>0 and not(success):
        if conn:
          # TODO: Add support to promote remote slaves (via SSH)
          print( "[%s] Slave %s is up. Attempting to promote it." % (datetime.datetime.now(), n['name']) )
          mylog.info( "Slave %s is up. Attempting to promote it." % n['name'] )
          if self.promot_mode == "auto":
              success = self._promote_slave()
          reconnect_attempts = reconnect_attempts - 1
          time.sleep( self.reconnect_interval )
          self.recovered = success

      if conn:
        conn.close()
                    
                
    def check_node(self, node):
        conn = None
        reconnect_attempts = self.reconnect_attempts
        err = None

        while reconnect_attempts>0 and not(conn):
            try:
                engine = node.get( 'engine' , None )
                if not(engine):
                  engine = create_engine( node.get( 'conninfo' ) )
                  node[ 'engine' ] = engine

                conn = engine.connect()
                res = conn.execute( 'SELECT current_timestamp' )
                res.close()
                err = None
            except Exception as e :
                reconnect_attempts=reconnect_attempts-1
                time.sleep( self.reconnect_interval )
                conn = None
                
                err = e
                
        if err:
            import traceback
            print( err )
            traceback.print_exc( err )
            mylog.error( "Exception", exc_info=1 )
        
        return conn

def initLogging(loglevel=logging.DEBUG, logmodule="pgmonitor"):
    global mylog

    l = logging.getLogger( logmodule )
    l.setLevel( loglevel )
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    logdir = os.environ.get( 'PGMON_LOG_DIR', '.' )
    logfile = os.path.join( logdir, "%s.log" % logmodule )
    fh = logging.FileHandler( logfile )
    fh.setLevel(loglevel)
    fh.setFormatter( formatter )
    print( "Logging to %s" % logfile )

    ch = logging.StreamHandler()
    ch.setLevel(loglevel)
    ch.setFormatter(formatter)

    l.addHandler(fh)
    l.addHandler(ch)

    mylog = l
    print("mylog is set.")
    
    
def main():
    global mylog
    initLogging()
    ap = argparse.ArgumentParser(description='Bluestone PG Monitor Tool.')
    ap.add_argument('-q','--quiet', help='Reduce the verbosity of the tool.', default="false")
    ap.add_argument('-c','--configfile', help='Specify the location of the config file.', default='pg_monitor.conf')
    ap.add_argument('-p','--pidfile', help="Specify the location of the pidfile.', default='pgmon.pid" )

    args = ap.parse_args()

    quiet = True if args.quiet == "true" else False
    

    if os.environ.get( 'PGMON_PID', None) :
        pgmonPid = os.environ[ 'PGMON_PID' ]
        (piddir, pidfile) = os.path.split( pgmonPid )
        (pidname, ext) = os.path.splitext( pidfile )

        pid.DEFAULT_PID_DIR = piddir
        
        try:
          with pid.PidFile( pidname=pidname ):
            mon = PgClusterMonitor(args.configfile, quiet=quiet)
            mylog.info( "pg_monitor started with pidfile: PGMON_PID=%s" % pgmonPid )
            mon.monitor()
        except pid.PidFileAlreadyLockedError:
            mylog.error( "Unable to create pidfile=%s! Is another pg_monitor running?" % pgmonPid )
            raise
    else :
        # Don't use PID file locking.
        mylog.info( "pg_monitor stated without a pid file." )
        mon = PgClusterMonitor(args.configfile, quiet=quiet)
        mon.monitor()
    
    
if __name__ == '__main__':
    main()
