#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import sys
import re
import time
import signal
from ansi2html import Ansi2HTMLConverter
from Queue import Queue, Empty
import threading
import subprocess

from shellstream.utils.io import *
from shellstream.transport import TransportError
from shellstream import BASH_PROMPT


class StreamWriter(threading.Thread):
    chunk_size = 7167

    @classmethod
    def write(cls, queue, transport, stream_id):
        instance = cls()
        instance.queue = queue
        instance.transport = transport
        instance.stream_id = stream_id
        instance.daemon = True
        instance.buffer = ""
        instance.last_write = time.time() - 60
        instance.start()
        return instance

    def run(self):
        self.do_work()

    def do_work(self):
        while True:
            try:
                lines = self.queue.get(False)
            except Empty:
                self.write_to_stream()
                lines = ""
                time.sleep(3)
                continue
            else:
                if lines:
                    self.buffer += lines
                    self.write_to_stream()

    def write_to_stream(self):
        # TODO: We are dropping extra large chunks here...
        chunk = self.buffer[:self.chunk_size]
        if len(chunk) and time.time() - self.last_write > 3:
            data = {}
            data["stream"] = self.stream_id
            data["content"] = chunk
            try:
                self.transport.fetch("api/stream/write/", data)
            except TransportError, e:
                print_red("\n{}".format(e))
                # TODO: need to exit process
                sys.exit()
            else:
                self.buffer = ""
                self.last_write = time.time()


class ShellReader(threading.Thread):
    span_regex = re.compile(r'</?span(.*?)>')
    pre_regex = re.compile(r'<pre>(.*?)</pre>')

    @classmethod
    def read(cls, queue, f_name):
        instance = cls()
        instance.queue = queue
        instance.f_name = f_name
        instance.conv = Ansi2HTMLConverter()
        instance.daemon = True
        instance.start()
        return instance

    def run(self):
        self.do_work()

    def do_work(self):

        def write(lines):
            self.queue.put(" ".join(lines))
            lines = []
            return lines, time.time()

        lines = []
        last_write = time.time()
        for line in self.tail():
            if line:
                html = self.parse_line(line)
                lines.append(html)
                lines, last_write = write(lines)
            else:
                lines, last_write = write(lines)

    def tail(self):
        p = subprocess.Popen(["tail", "-f", self.f_name], stdout=subprocess.PIPE)
        while 1:
            time.sleep(.5)
            line = p.stdout.readline()
            yield line

    def parse_line(self, line):
        # differentiate here between user input and output
        html = self.conv.convert(ansi=line, full=False).strip("\r\n").replace("\x1b[?1034h", "")
        # html = self.escape_html(html)
        html = self.remove_undos(html)
        _class = "bash-input" if BASH_PROMPT in html else "bash-output"
        return '<pre class="{}">{}</pre>'.format(_class, html)

    def remove_undos(self, html):
        _buffer = []
        for char in html:
            if char == "\x08":
                _buffer.pop()
            else:
                _buffer.append(char)
        return "".join(_buffer)

    def pad_input(self, html):
        return '[div] class="shell-input"[--]{}[/div][div] class="shell-output[--][pre]'.format(html)

    def close_padding(self):
        return '[/pre][/div]'

    def escape_html(self, html):
        partial_clean = self.escape_span(html)
        full_clean = self.escape_pre(partial_clean)
        return full_clean

    def escape_span(self, html):
        def replace_it(object):
            if object.group() == "</span>":
                return "[/sp]"
            else:
                return "[sp]{}[--]".format(object.group(1))
        return re.sub(self.span_regex, replace_it, html)

    def escape_pre(self, html):
        def replace_it(object):
            filler = object.group(1)
            if not filler or filler == "\x08":
                return ""
            else:
                return "[pr]{}[/pr]".format(filler)

        return re.sub(self.pre_regex, replace_it, html)


class Worker(object):

    @classmethod
    def labor(self, transport, f_name, main_pid, stream_id):
        queue = Queue()
        reader = ShellReader.read(queue, f_name)
        writer = StreamWriter.write(queue, transport, stream_id)
        while True:
            time.sleep(2)
            if not (reader.is_alive() and writer.is_alive()):
                try:
                    os.kill(main_pid, signal.SIGQUIT)
                except OSError:
                    pass
                finally:
                    break
