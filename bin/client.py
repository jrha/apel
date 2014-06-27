#!/usr/bin/env python

#   Copyright (C) 2012 STFC
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

#   Main script for APEL client.
#   The order of execution is as follows:
#    - fetch benchmark information from LDAP database
#    - join EventRecords and BlahdRecords into JobRecords
#    - summarise jobs
#    - unload JobRecords or SummaryRecords into filesystem
#    - send data to server using SSM
'''
   @author: Konrad Jopek, Will Rogers
'''

from optparse import OptionParser
import ConfigParser
import sys
import os
import logging.config
import ldap

from apel import __version__
from apel.db import ApelDb, ApelDbException
from apel.db.unloader import DbUnloader
from apel.ldap import fetch_specint
from apel.common import set_up_logging
from apel.common.exceptions import install_exc_handler, default_handler

from ssm.brokers import StompBrokerGetter, STOMP_SERVICE, STOMP_SSL_SERVICE
from ssm.ssm2 import Ssm2, Ssm2Exception


DB_BACKEND = 'mysql'
LOGGER_ID = 'client'
LOG_BREAK = '====================='


class ClientConfigException(Exception):
    '''
    Exception raised if client is misconfigured.
    '''
    pass


def run_ssm(scp):
    '''
    Run the SSM according to the values in the ConfigParser object.
    '''
    log = logging.getLogger(LOGGER_ID)
    try:
        bg = StompBrokerGetter(scp.get('broker', 'bdii'))
        use_ssl = scp.getboolean('broker', 'use_ssl')
        if use_ssl:
            service = STOMP_SSL_SERVICE
        else:
            service = STOMP_SERVICE
        brokers = bg.get_broker_hosts_and_ports(service, scp.get('broker',
                                                                 'network'))
        log.info('Found %s brokers.' % len(brokers))
    except ConfigParser.NoOptionError, e:
        try:
            host = scp.get('broker', 'host')
            port = scp.get('broker', 'port')
            brokers = [(host, int(port))]
        except ConfigParser.NoOptionError:
            log.error('Options incorrectly supplied for either single broker '
                      'or broker network. Please check configuration.')
            log.error('System will exit.')
            log.info()
            print 'SSM failed to start.  See log file for details.'
            sys.exit(1)
    except ldap.LDAPError, e:
        log.error('Failed to retrieve brokers from LDAP: %s' % str(e))
        log.error('Messages were not sent.')
        return

    try:
        try:
            server_cert = scp.get('certificates', 'server_cert')
            if not os.path.isfile(server_cert):
                raise Ssm2Exception('Server certificate location incorrect.')
        except ConfigParser.NoOptionError:
            log.info('No server certificate supplied. Will not encrypt messages.')
            server_cert = None

        try:
            destination = scp.get('messaging', 'destination')
            if destination == '':
                raise Ssm2Exception('No destination queue is configured.')
        except ConfigParser.NoOptionError, e:
            raise Ssm2Exception(e)

        ssm = Ssm2(brokers,
                   scp.get('messaging', 'path'),
                   dest=scp.get('messaging', 'destination'),
                   cert=scp.get('certificates', 'certificate'),
                   capath=scp.get('certificates', 'capath'),
                   key=scp.get('certificates', 'key'),
                   use_ssl=scp.getboolean('broker', 'use_ssl'),
                   enc_cert=server_cert)
    except Ssm2Exception, e:
        log.error('Failed to initialise SSM: %s' % str(e))
        log.error('Messages have not been sent.')
        return

    try:
        ssm.handle_connect()
        ssm.send_all()
        ssm.close_connection()
    except Ssm2Exception, e:
        log.error('SSM failed to complete successfully: %s' % str(e))
        return

    log.info('SSM run has finished.')
    log.info('Sending SSM has shut down.')


def run_client(ccp):
    '''
    Run the client according to the configuration in the ConfigParser
    object.
    '''
    log = logging.getLogger(LOGGER_ID)

    try:
        spec_updater_enabled = ccp.getboolean('spec_updater', 'enabled')
        joiner_enabled = ccp.getboolean('joiner', 'enabled')

        if spec_updater_enabled or joiner_enabled:
            site_name = ccp.get('spec_updater', 'site_name')
            if site_name == '':
                raise ClientConfigException('Site name must be configured.')

        if spec_updater_enabled:
            ldap_host = ccp.get('spec_updater', 'ldap_host')
            ldap_port = int(ccp.get('spec_updater', 'ldap_port'))
        local_jobs = ccp.getboolean('joiner', 'local_jobs')
        if local_jobs:
            hostname = ccp.get('spec_updater', 'lrms_server')
            if hostname == '':
                raise ClientConfigException('LRMS server hostname must be '
                                            'configured if local jobs are '
                                            'enabled.')

            slt = ccp.get('spec_updater', 'spec_type')
            sl = ccp.getfloat('spec_updater', 'spec_value')

        unloader_enabled = ccp.getboolean('unloader', 'enabled')

        include_vos = None
        exclude_vos = None
        if unloader_enabled:
            unload_dir = ccp.get('unloader', 'dir_location')
            if ccp.getboolean('unloader', 'send_summaries'):
                table_name = 'VSuperSummaries'
            else:
                table_name = 'VJobRecords'
            send_ur = ccp.getboolean('unloader', 'send_ur')
            try:
                include = ccp.get('unloader', 'include_vos')
                include_vos = [vo.strip() for vo in include.split(',')]
            except ConfigParser.NoOptionError:
                # Only exclude VOs if we haven't specified the ones to include.
                include_vos = None
                try:
                    exclude = ccp.get('unloader', 'exclude_vos')
                    exclude_vos = [vo.strip() for vo in exclude.split(',')]
                except ConfigParser.NoOptionError:
                    exclude_vos = None

    except (ClientConfigException, ConfigParser.Error), err:
        log.error('Error in configuration file: ' + str(err))
        sys.exit(1)

    log.info('Starting apel client version %s.%s.%s' % __version__)

    # Log into the database
    try:
        db_hostname = ccp.get('db', 'hostname')
        db_port = ccp.getint('db', 'port')
        db_name = ccp.get('db', 'name')
        db_username = ccp.get('db', 'username')
        db_password = ccp.get('db', 'password')

        log.info('Connecting to the database ... ')
        db = ApelDb(DB_BACKEND, db_hostname, db_port,
                    db_username, db_password, db_name)
        db.test_connection()
        log.info('Connected.')

    except (ConfigParser.Error, ApelDbException), err:
        log.error('Error during connecting to database: ' + str(err))
        log.info(LOG_BREAK)
        sys.exit(1)

    if spec_updater_enabled:
        log.info(LOG_BREAK)
        log.info('Starting spec updater.')
        try:
            spec_values = fetch_specint(site_name, ldap_host, ldap_port)
            for value in spec_values:
                db.update_spec(site_name, value[0], 'si2k', value[1])
            log.info('Spec updater finished.')
        except ldap.SERVER_DOWN, e:
            log.warn('Failed to fetch spec info: %s' % e)
            log.warn('Spec updater failed.')
        except ldap.NO_SUCH_OBJECT, e:
            log.warn('Found no spec values in BDII: %s' % e)
            log.warn('Is the site name %s correct?' % site_name)

        log.info(LOG_BREAK)

    if joiner_enabled:
        log.info(LOG_BREAK)
        log.info('Starting joiner.')
        # This contains all the joining logic, contained in ApelMysqlDb() and
        # the stored procedures.
        if local_jobs:
            log.info('Updating benchmark information for local jobs:')
            log.info('%s, %s, %s, %s.' % (site_name, hostname, slt, sl))
            db.update_spec(site_name, hostname, slt, sl)
            log.info('Creating local jobs.')
            db.create_local_jobs()

        db.join_records()
        log.info('Joining complete.')
        log.info(LOG_BREAK)

    # Always summarise - we need the summaries for the sync messages.
    log.info(LOG_BREAK)
    log.info('Starting summariser.')
    # This contains all the summarising logic, contained in ApelMysqlDb() and
    # the stored procedures.
    db.summarise_jobs()
    log.info('Summarising complete.')
    log.info(LOG_BREAK)

    if unloader_enabled:
        log.info(LOG_BREAK)
        log.info('Starting unloader.')

        log.info('Will unload from %s.' % table_name)

        interval = ccp.get('unloader', 'interval')
        withhold_dns = ccp.getboolean('unloader', 'withhold_dns')

        unloader = DbUnloader(db, unload_dir, include_vos, exclude_vos,
                              local_jobs, withhold_dns)
        try:
            if interval == 'latest':
                msgs, recs = unloader.unload_latest(table_name, send_ur)
            elif interval == 'gap':
                start = ccp.get('unloader', 'gap_start')
                end = ccp.get('unloader', 'gap_end')
                msgs, recs = unloader.unload_gap(table_name, start, end, send_ur)
            elif interval == 'all':
                msgs, recs = unloader.unload_all(table_name, send_ur)
            else:
                log.warn('Unrecognised interval: %s' % interval)
                log.warn('Will not start unloader.')

            log.info('Unloaded %d records in %d messages.' % (recs, msgs))

        except KeyError:
            log.warn('Invalid table name: %s, omitting' % table_name)
        except ApelDbException, e:
            log.warn('Failed to unload records successfully: %s' % str(e))

        # Always send sync messages
        msgs, recs = unloader.unload_sync()

        log.info('Unloaded %d sync records in %d messages.' % (recs, msgs))

        log.info('Unloading complete.')
        log.info(LOG_BREAK)


def main():
    '''
    Parse command line arguments, set up logging and begin the client
    workflow.
    '''
    install_exc_handler(default_handler)
    ver = 'APEL client %s.%s.%s' % __version__
    opt_parser = OptionParser(version=ver, description=__doc__)

    opt_parser.add_option('-c', '--config',
                          help='main configuration file for APEL',
                          default='/etc/apel/client.cfg')

    opt_parser.add_option('-s', '--ssm_config',
                          help='location of SSM config file',
                          default='/etc/apel/sender.cfg')

    opt_parser.add_option('-l', '--log_config',
                          help='location of logging config file (optional)',
                          default='/etc/apel/logging.cfg')

    options, unused_args = opt_parser.parse_args()
    ccp = ConfigParser.ConfigParser()
    ccp.read(options.config)

    scp = ConfigParser.ConfigParser()
    scp.read(options.ssm_config)

    # set up logging
    try:
        if os.path.exists(options.log_config):
            logging.config.fileConfig(options.log_config)
        else:
            set_up_logging(ccp.get('logging', 'logfile'),
                           ccp.get('logging', 'level'),
                           ccp.getboolean('logging', 'console'))
        log = logging.getLogger(LOGGER_ID)
    except (ConfigParser.Error, ValueError, IOError), err:
        print 'Error configuring logging: %s' % str(err)
        print 'The system will exit.'
        sys.exit(1)

    run_client(ccp)

    if ccp.getboolean('ssm', 'enabled'):
        # Send unloaded messages
        log.info(LOG_BREAK)
        log.info('Starting SSM.')
        run_ssm(scp)
        log.info('SSM stopped.')
        log.info(LOG_BREAK)

    log.info('Client finished')


if __name__ == '__main__':
    main()
