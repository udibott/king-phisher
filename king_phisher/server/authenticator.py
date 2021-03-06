#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  king_phisher/server/authenticator.py
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

import grp
import hashlib
import json
import logging
import os
import pwd
import random
import string
import time

from king_phisher.third_party import pam

__all__ = ['ForkedAuthenticator']

make_salt = lambda: ''.join(random.choice(string.ascii_letters + string.digits + string.punctuation) for x in range(random.randint(5, 8)))
make_hash = lambda pw: hashlib.sha512(pw.encode('utf-8')).digest()

def get_groups_for_user(username):
	"""
	Get the groups that a user is a member of.

	:param str username: The user to lookup group membership for.
	:rtype: set
	:return: The names of the groups that the user is a member of.
	"""
	groups = set(g.gr_name for g in grp.getgrall() if username in g.gr_mem)
	groups.add(grp.getgrgid(pwd.getpwnam(username).pw_gid).gr_name)
	return groups

class ForkedAuthenticator(object):
	"""
	This provides authentication services to the King Phisher server
	through PAM. It is initialized while the server is running as root
	and forks into the background before the privileges are dropped. It
	continues to run as root and forwards requests through a pipe to PAM.
	The pipes use JSON to encoded the request data as a string before
	sending it and using a newline character as the terminator.
	"""
	def __init__(self, cache_timeout=600, required_group=None):
		"""
		:param int cache_timeout: The life time of cached credentials in seconds.
		:param str required_group: A group that if specified, users must be a member of to be authenticated.
		"""
		self.logger = logging.getLogger('KingPhisher.Server.Authenticator')
		self.cache_timeout = cache_timeout
		"""The timeout of the credential cache in seconds."""
		self.required_group = required_group
		if self.required_group and not self.required_group in [g.gr_name for g in grp.getgrall()]:
			self.logger.error('the specified group for authentication was not found')
		self.parent_rfile, self.child_wfile = os.pipe()
		self.child_rfile, self.parent_wfile = os.pipe()
		self.child_pid = os.fork()
		"""The PID of the forked child."""
		if not self.child_pid:
			self.rfile = self.child_rfile
			self.wfile = self.child_wfile
		else:
			self.rfile = self.parent_rfile
			self.wfile = self.parent_wfile
		self.rfile = os.fdopen(self.rfile, 'r', 1)
		self.wfile = os.fdopen(self.wfile, 'w', 1)
		if not self.child_pid:
			self.child_routine()
			self.rfile.close()
			self.wfile.close()
			logging.shutdown()
			os._exit(os.EX_OK)
		self.cache_salt = make_salt()
		"""The salt to be prepended to passwords before hashing them for the cache."""
		self.cache = {}
		"""The credential cache dictionary. Keys are usernames and values are tuples of password hashes and ages."""
		return

	def send(self, request):
		"""
		Encode and send a request through the pipe to the opposite end.

		:param dict request: A request.
		"""
		self.wfile.write(json.dumps(request) + '\n')

	def recv(self):
		"""
		Receive a request and decode it.

		:return: The decoded request.
		:rtype: dict
		"""
		try:
			request = self.rfile.readline()[:-1]
			return json.loads(request)
		except KeyboardInterrupt:
			return {}

	def child_routine(self):
		"""
		The main routine that is executed by the child after the object
		forks. This loop does not exit unless a stop request is made.
		"""
		service = 'login'
		if os.path.isfile('/etc/pam.d/sshd'):
			service = 'sshd'
		while True:
			request = self.recv()
			if not 'action' in request:
				continue
			action = request['action']
			if action == 'stop':
				break
			elif action != 'authenticate':
				continue
			username = str(request['username'])
			password = str(request['password'])
			result = {}
			result['result'] = pam.authenticate(username, password, service=service)
			if result['result'] and self.required_group:
				result['result'] = False
				try:
					assert self.required_group in get_groups_for_user(username)
				except AssertionError:
					self.logger.warning("authentication failed for user: {0} reason: lack of group membership".format(username))
				except KeyError:
					self.logger.error("encountered a KeyError while looking up group member ship for user: {0}".format(username))
				else:
					result['result'] = True
			else:
				self.logger.warning("authentication failed for user: {0} reason: bad username or password".format(username))
			self.send(result)

	def authenticate(self, username, password):
		"""
		Check if a username and password are valid. If they are, the
		password will be salted, hashed with SHA-512 and stored so the
		next call with the same values will not require sending a
		request to the forked child.

		:param str username: The username to check.
		:param str password: The password to check.
		:return: Whether the credentials are valid or not.
		:rtype: bool
		"""
		pw_hash = make_hash(self.cache_salt + password)
		cached_hash, timeout = self.cache.get(username, (None, 0))
		if timeout < time.time():
			request = {}
			request['action'] = 'authenticate'
			request['username'] = username
			request['password'] = password
			self.send(request)
			result = self.recv()
			if result['result']:
				self.cache[username] = (pw_hash, time.time() + self.cache_timeout)
			return result['result']
		return cached_hash == pw_hash

	def stop(self):
		"""
		Send a stop request to the child process and wait for it to exit.
		"""
		if not os.path.exists("/proc/{0}".format(self.child_pid)):
			return
		request = {}
		request['action'] = 'stop'
		self.send(request)
		os.waitpid(self.child_pid, 0)
		self.rfile.close()
		self.wfile.close()
