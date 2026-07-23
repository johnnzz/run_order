"""Persistent ExifTool subprocess for batch metadata reads and writes."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from types import SimpleNamespace

from _graceful_interrupt import abort_if_interrupt_requested, interrupt_requested

logger = logging.getLogger(__name__)


class ExifToolSession:
	"""One long-lived ``exiftool -stay_open`` process for a batch run."""

	def __init__(self, executable: str = "exiftool"):
		self.executable = executable
		self._proc: subprocess.Popen | None = None
		self._lock = threading.Lock()

	def __enter__(self):
		self.start()
		return self

	def __exit__(self, exc_type, exc, tb):
		del exc_type, exc, tb
		self.close()

	def start(self):
		if self._proc is not None:
			return
		try:
			self._proc = subprocess.Popen(
				[self.executable, "-stay_open", "True", "-@", "-"],
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				stderr=subprocess.PIPE,
			)
		except FileNotFoundError as exc:
			raise FileNotFoundError(
				"exiftool is not installed or not on PATH"
			) from exc

	def close(self):
		proc = self._proc
		self._proc = None
		if proc is None:
			return
		try:
			if proc.stdin:
				proc.stdin.write(b"-stay_open\nFalse\n-execute\n")
				proc.stdin.flush()
		except OSError:
			pass
		try:
			proc.wait(timeout=30)
		except subprocess.TimeoutExpired:
			proc.kill()

	def _read_until_ready(self) -> bytes:
		if self._proc is None or self._proc.stdout is None:
			raise RuntimeError("ExifTool session is not running")
		lines = []
		while True:
			line = self._proc.stdout.readline()
			if not line:
				if interrupt_requested():
					abort_if_interrupt_requested()
				raise RuntimeError("ExifTool closed stdout unexpectedly")
			if line.strip() == b"{ready}":
				break
			lines.append(line)
		return b"".join(lines)

	def execute(self, args: list[str]) -> SimpleNamespace:
		"""Send arguments plus ``-execute``; return stdout payload and stderr."""
		try:
			with self._lock:
				if self._proc is None or self._proc.stdin is None:
					raise RuntimeError("ExifTool session is not running")
				for arg in args:
					self._proc.stdin.write(arg.encode("utf-8"))
					self._proc.stdin.write(b"\n")
				self._proc.stdin.write(b"-execute\n")
				self._proc.stdin.flush()
			stdout = self._read_until_ready()
		except SystemExit:
			raise
		except OSError:
			if interrupt_requested():
				abort_if_interrupt_requested()
			raise
		text_out = stdout.decode("utf-8", errors="replace").strip()
		return SimpleNamespace(
			stdout=text_out,
			stderr="",
			returncode=1 if "Error:" in text_out else 0,
		)

	def read_json(self, filename: str, tags: list[str] | tuple[str, ...]):
		tag_args = ["-{}".format(tag) for tag in tags]
		result = self.execute(["-json"] + tag_args + [filename])
		if result.returncode != 0 and not result.stdout:
			return None
		if not result.stdout:
			return None
		return json.loads(result.stdout)[0]

	def read_json_batch(self, filenames: list[str], tags: list[str] | tuple[str, ...]):
		if not filenames:
			return []
		tag_args = ["-{}".format(tag) for tag in tags]
		result = self.execute(["-json"] + tag_args + filenames)
		if not result.stdout:
			return [None] * len(filenames)
		parsed = json.loads(result.stdout)
		by_source = {}
		for item in parsed:
			source = item.get("SourceFile")
			if source:
				by_source[source] = item
				by_source[source.replace("\\", "/")] = item
		output = []
		for filename in filenames:
			item = by_source.get(filename)
			if item is None:
				normalized = filename.replace("\\", "/")
				item = by_source.get(normalized)
			output.append(item)
		return output

	def write(self, args: list[str], filename: str):
		return self.execute(args + [filename])
