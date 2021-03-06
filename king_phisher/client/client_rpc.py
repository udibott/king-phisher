#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  king_phisher/client/client_rpc.py
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are
#  met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following disclaimer
#    in the documentation and/or other materials provided with the
#    distribution.
#  * Neither the name of the project nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
#  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

import code
import getpass
import json
import logging
import os
import sys

from king_phisher import find
from king_phisher.third_party import AdvancedHTTPServer
try:
	import msgpack # pylint: disable=unused-import
	has_msgpack = True
	"""Whether the :py:mod:`msgpack` module is available or not."""
except ImportError:
	has_msgpack = False

class KingPhisherRPCClient(AdvancedHTTPServer.AdvancedHTTPServerRPCClientCached):
	"""
	The main RPC object for communicating with the King Phisher Server
	over RPC.
	"""
	def __init__(self, *args, **kwargs):
		self.logger = logging.getLogger('KingPhisher.Client.RPC')
		super(KingPhisherRPCClient, self).__init__(*args, **kwargs)
		if has_msgpack:
			serializer = 'binary/message-pack'
		else:
			serializer = 'binary/json'
		self.set_serializer(serializer)

	def remote_table(self, table, *args):
		"""
		Get a remote table from the server by calling the correct RPC
		method.

		:param str table: The table name to retrieve.
		:return: A generator which yields rows in dictionaries.
		"""
		table_method = table + '/view'
		page = 0
		args = list(args)
		args.append(page)
		results = self.call(table_method, *args)
		results_length = len(results or '')
		while results:
			columns = results['columns']
			for row in results['rows']:
				yield dict(zip(columns, row))
			if len(results) < results_length:
				break
			args[-1] += 1
			results = self.call(table_method, *args)

	def remote_table_row(self, table, row_id, cache=False, refresh=False):
		"""
		Get a specific row by it's id, optionally cacheing it.

		:param str table: The table in which the row exists.
		:param row_id: The value of the row's id column.
		:param bool cache: Whether to use the cache for this row.
		:param bool refresh: If *cache* is True, get the current row value and store it.
		:return: The remote row.
		"""
		table_method = table + '/get'
		if cache and refresh:
			result = self.cache_call_refresh(table_method, row_id)
		elif cache and not refresh:
			result = self.cache_call(table_method, row_id)
		else:
			result = self.call(table_method, row_id)
		return result

def vte_child_routine(config):
	"""
	This is the method which is executed within the child process spawned
	by VTE. It expects additional values to be set in the *config*
	object so it can initialize a new :py:class:`.KingPhisherRPCClient`
	instance. It will then drop into an interpreter where the user may directly
	interact with the rpc object.

	:param str config: A JSON encoded client configuration.
	"""
	config = json.loads(config)
	try:
		import readline
		import rlcompleter # pylint: disable=unused-variable
	except ImportError:
		pass
	else:
		readline.parse_and_bind('tab: complete')
	plugins_directory = find.find_data_directory('plugins')
	if plugins_directory:
		sys.path.append(plugins_directory)

	rpc = KingPhisherRPCClient(**config['rpc_data'])
	logged_in = False
	for _ in range(0, 3):
		rpc.password = getpass.getpass("{0}@{1}'s password: ".format(rpc.username, rpc.host))
		try:
			logged_in = rpc('ping')
		except AdvancedHTTPServer.AdvancedHTTPServerRPCError:
			print('Permission denied, please try again.')
			continue
		else:
			break
	if not logged_in:
		return

	banner = "Python {0} on {1}".format(sys.version, sys.platform)
	print(banner)
	information = "Campaign Name: '{0}'  ID: {1}".format(config['campaign_name'], config['campaign_id'])
	print(information)
	console_vars = {
		'CAMPAIGN_NAME': config['campaign_name'],
		'CAMPAIGN_ID': config['campaign_id'],
		'os': os,
		'rpc': rpc,
		'sys': sys
	}
	export_to_builtins = ['CAMPAIGN_NAME', 'CAMPAIGN_ID', 'rpc']
	console = code.InteractiveConsole(console_vars)
	for var in export_to_builtins:
		console.push("__builtins__['{0}'] = {0}".format(var))
	console.interact('The \'rpc\' object holds the connected KingPhisherRPCClient instance')
	return
