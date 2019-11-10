import abc
import collections.abc
import io
import typing

import trio.abc


T    = typing.TypeVar("T")
T_co = typing.TypeVar("T_co", covariant=True)


ArbitraryReceiveChannel = typing.Union[
	trio.abc.ReceiveChannel[T_co],
	typing.AsyncIterable[T_co],
	typing.Awaitable[T_co],
	typing.Iterable[T_co]
]


ArbitraryReceiveStream = typing.Union[
	trio.abc.ReceiveStream,
	typing.AsyncIterable[bytes],
	typing.Awaitable[bytes],
	typing.Iterable[bytes],
	bytes
]


class _ChannelSharedBase:
	__slots__ = ("lock", "refcount")
	
	lock:     trio.Lock
	refcount: int
	
	def __init__(self):
		self.lock     = trio.Lock()
		self.refcount = 1


class ReceiveChannel(trio.abc.ReceiveChannel[T_co], typing.Generic[T_co]):
	"""A slightly extended version of `trio`'s standard interface for receiving object streams.
	
	Attributes
	----------
	count
		The number of objects that will be returned, or `None` if unavailable
	atime
		Time of the entry's last access (before the current one) in seconds
		since the Unix epoch, or `None` if unkown
	mtime
		Time of the entry's last modification in seconds since the Unix epoch,
		or `None` if unknown
	btime
		Time of entry creation in seconds since the Unix epoch, or `None`
		if unknown
	"""
	__slots__ = ("count", "atime", "mtime", "btime")
	
	# The total length of this stream (if available)
	count: typing.Optional[int]
	
	# The backing record's last access time
	atime: typing.Optional[typing.Union[int, float]]
	# The backing record's last modification time
	mtime: typing.Optional[typing.Union[int, float]]
	# The backing record's creation (“birth”) time
	btime: typing.Optional[typing.Union[int, float]]
	
	
	def __init__(self):
		self.count = None
		self.atime = None
		self.mtime = None
		self.btime = None
	
	
	async def collect(self) -> typing.List[T_co]:
		result: typing.List[T_co] = []
		async with self:
			async for item in self:
				result.append(item)
		return result


class _WrapingChannelShared(_ChannelSharedBase, typing.Generic[T_co]):
	__slots__ = ("source",)
	
	source: typing.Union[None, typing.AsyncIterator[T_co], typing.Iterator[T_co]]


class WrapingReceiveChannel(ReceiveChannel[T_co], typing.Generic[T_co]):
	"""Abstracts over various forms of synchronous and asynchronous returning of
	   object streams
	"""
	__slots__ = ("_shared", "_closed")
	
	_closed: bool
	_shared: _WrapingChannelShared[T_co]
	
	def __init__(self, source: ArbitraryReceiveChannel[T_co], *,
	             _shared: typing.Optional[_WrapingChannelShared[T_co]] = None):
		super().__init__()
		
		if _shared is not None:
			self._shared = _shared
		
		source_val: typing.Union[typing.AsyncIterable[T_co], typing.Iterable[T_co]]
		
		# Handle special cases, so that we'll end up either with a synchronous
		# or an asynchrous iterable (also tries to calculate the expected total
		# number of objects ahead of time for some known cases)
		if isinstance(source, collections.abc.Awaitable):
			async def await_iter_wrapper(source):
				yield await source
			source_val = await_iter_wrapper(source)
		elif isinstance(source, collections.abc.Sequence):
			self.count = len(source)
			source_val = source
		else:
			source_val = source
		assert isinstance(source_val, (collections.abc.AsyncIterable, collections.abc.Iterable))
		
		self._closed = False
		self._shared = _WrapingChannelShared()
		if isinstance(source_val, collections.abc.AsyncIterable):
			self._shared.source = source_val.__aiter__()
		else:
			self._shared.source = iter(source_val)
	
	
	async def receive(self) -> T_co:
		if self._closed:
			raise trio.ClosedResourceError()
		if self._shared.source is None:
			raise trio.EndOfChannel()
		
			try:
			async with self._shared.lock:  # type: ignore[attr-defined]  # upstream type bug
				if isinstance(self._shared.source, collections.abc.AsyncIterator):
					try:
						return await self._shared.source.__anext__()
			except StopAsyncIteration as exc:
				raise trio.EndOfChannel() from exc
		else:
			try:
						return next(self._shared.source)
			except StopIteration as exc:
				raise trio.EndOfChannel() from exc
		except trio.BrokenResourceError:
			await self.aclose(_mark_closed=True)
			raise
		except trio.EndOfChannel:
			await self.aclose(_mark_closed=False)
			raise
	
	
	def receive_nowait(self) -> T_co:
		if self._closed:
			raise trio.ClosedResourceError()
		if self._shared.source is None:
			raise trio.EndOfChannel()
		
		self._shared.lock.acquire_nowait()
		try:
			if isinstance(self._shared.source, trio.abc.ReceiveChannel):
				try:
					return self._shared.source.receive_nowait()
				except (trio.EndOfChannel, trio.BrokenResourceError):
					# We cannot handle invoking async close here
					raise trio.WouldBlock() from None
			elif isinstance(self._shared.source, collections.abc.AsyncIterator):
				# Cannot ask this stream type for a non-blocking value
				raise trio.WouldBlock()
			else:
				try:
					return next(self._shared.source)
				except StopIteration:
					# We cannot handle invoking async close here
					raise trio.WouldBlock() from None
		finally:
			self._shared.lock.release()
	
	
	def clone(self) -> ReceiveChannel[T_co]:
		if self._closed:
			raise trio.ClosedResourceError()
		
		if isinstance(self._shared.source, trio.abc.ReceiveChannel):
			return WrapingReceiveChannel(self._shared.source.clone())
		else:
			try:
				# Cast source value to ignore the possible `None` variant as the
				# passed source value will be ignored if we provide `_shared`
				source = typing.cast(trio.abc.ReceiveChannel[T_co], self._shared.source)
				
				return WrapingReceiveChannel(source, _shared=self._shared)
			except BaseException:
				raise
			else:
				self._shared.refcount += 1
	
	
	async def aclose(self, *, _mark_closed: bool = True) -> None:
		if not self._closed and _mark_closed:
			self._closed = True
		
		if self._shared.source is None:
			return
		
		self._shared.refcount -= 1
		if self._shared.refcount != 0:
			return
		
		try:
			if isinstance(self._shared.source, collections.abc.AsyncIterator):
				await self._shared.source.aclose()  # type: ignore  # We catch errors instead
			else:
				self._shared.source.close()  # type: ignore  # We catch errors instead
		except AttributeError:
			pass
		finally:
			self._shared.source = None


def receive_channel_from(channel: ArbitraryReceiveChannel[T_co]) -> ReceiveChannel[T_co]:
	return WrapingReceiveChannel(channel)



class ReceiveStream(trio.abc.ReceiveStream):
	"""A slightly extended version of `trio`'s standard interface for receiving byte streams.
	
	Attributes
	----------
	size
		The size of the entire stream data in bytes, or `None` if unavailable
	atime
		Time of the entry's last access (before the current one) in seconds
		since the Unix epoch, or `None` if unkown
	mtime
		Time of the entry's last modification in seconds since the Unix epoch,
		or `None` if unknown
	btime
		Time of entry creation in seconds since the Unix epoch, or `None`
		if unknown
	"""
	__slots__ = ("size", "atime", "mtime", "btime")
	
	# The total length of this stream (if available)
	size: typing.Optional[int]
	
	# The backing record's last access time
	atime: typing.Optional[typing.Union[int, float]]
	# The backing record's last modification time
	mtime: typing.Optional[typing.Union[int, float]]
	# The backing record's creation (“birth”) time
	btime: typing.Optional[typing.Union[int, float]]
	
	
	def __init__(self):
		self.size  = None
		self.atime = None
		self.mtime = None
		self.btime = None
	
	
	async def collect(self) -> bytes:
		value = bytearray()
		async with self:
			# Use “size”, if available, to try and read the entire stream's conents
			# in one go
			max_bytes = getattr(self, "size", None)
			
			while True:
				chunk = await self.receive_some(max_bytes)
				if len(chunk) < 1:
					break
				value += chunk
		return bytes(value)



class WrapingReceiveStream(ReceiveStream):
	"""Abstracts over various forms of synchronous and asynchronous returning of
	   byte streams
	"""
	
	_buffer:  bytearray
	_memview: typing.Union[memoryview, None]
	_offset:  int
	
	_source: typing.Union[typing.AsyncIterator[bytes], typing.Iterator[bytes]]
	
	def __init__(self, source):
		super().__init__()
		
		# Handle special cases, so that we'll end up either with a synchronous
		# or an asynchrous iterable (also tries to calculate the expected total
		# stream size ahead of time for some known cases)
		source_val: typing.Union[trio.abc.ReceiveStream,
		                         typing.AsyncIterable[bytes],
		                         typing.Iterable[bytes]]
		if isinstance(source, collections.abc.Awaitable):
			async def await_iter_wrapper(source):
				yield await source
			source_val = await_iter_wrapper(source)
		elif isinstance(source, bytes):
			self.size = len(source)
			source_val = (source,)
		elif isinstance(source, collections.abc.Sequence):
			# Remind mypy that the result of the above test is
			# `Sequence[bytes]`, not `Sequence[Any]` in this case
			source = typing.cast(typing.Sequence[bytes], source)
			
			self.size = sum(len(item) for item in source)
			source_val = source
		elif isinstance(source, io.BytesIO):
			# Ask in-memory stream for its remaining length, restoring its
			# original state afterwards
			pos = source.tell()
			source.seek(0, io.SEEK_END)
			self.size = source.tell() - pos
			source.seek(pos, io.SEEK_SET)
			source_val = source
		else:
			source_val = source
		
		self._buffer  = bytearray()
		self._memview: typing.Optional[memoryview] = None
		self._offset  = 0
		if isinstance(source_val, trio.abc.ReceiveStream):
			self._source = source_val
		elif isinstance(source_val, collections.abc.AsyncIterable):
			self._source = source_val.__aiter__()
		else:
			self._source = iter(source_val)
	
	
	async def receive_some(self, max_bytes=None):
		# Serve chunks from buffer if there is any data that hasn't been
		# delivered yet
		if self._memview:
			if max_bytes is not None:
				end_offset = min(self._offset + max_bytes, len(self._memview))
			else:
				end_offset = len(self._memview)
			
			result = bytes(self._memview[self._offset:end_offset])
			if end_offset >= len(self._memview):
				self._offset = 0
				self._memview.release()
				self._memview = None
				self._buffer.clear()
			return result
		
		
		at_end = False
		while not at_end:
			value = b""
			if isinstance(self._source, trio.abc.ReceiveStream):
				# This branch is just an optimization to pass `max_bytes` along
				# to subordinated ReceiveStreams
				value = await self._source.receive_some(max_bytes)
				at_end = (len(value) < 1)
			elif isinstance(self._source, collections.abc.AsyncIterator):
				try:
					value = await self._source.__anext__()
				except StopAsyncIteration:
					at_end = True
			else:
				try:
					value = next(self._source)
				except StopIteration:
					at_end = True
			
			# Skip empty returned byte strings as they have a special meaning here
			if len(value) < 1:
				continue
			
			# Stash extra bytes that are too large for our receiver
			if max_bytes is not None and max_bytes > len(value):
				self._buffer += value[max_bytes:]
				self._memview = memoryview(self._buffer)
				value = value[:max_bytes]
			
			return value
		
		# We're at the end
		await self.aclose()
		return b""
	
	
	async def aclose(self):
		try:
			if isinstance(self._source, collections.abc.Iterable):
				await self._source.aclose()  # type: ignore  # We catch errors instead
			else:
				self._source.close()  # type: ignore  # We catch errors instead
		except AttributeError:
			pass
