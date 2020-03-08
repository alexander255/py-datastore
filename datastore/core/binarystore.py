import abc
import collections.abc
import typing

import trio

from . import key as key_
from . import query as query_


class util:  # noqa
	from .util import decorator
	from .util import metadata
	from .util import stream


def is_valid_value_type(value: util.stream.ArbitraryReceiveStream) -> bool:
	"""Checks that `value` is of the right type for `Datastore.put`
	
	It's just too easy to acidentally pass in the wrong type without this check.
	Unfortunately this cannot check whether iterators return the correct types,
	so the utility of this function unfortunately is limited to some extent.
	"""
	return isinstance(value, (
		trio.abc.ReceiveStream,
		collections.abc.AsyncIterable,
		collections.abc.Awaitable,
		collections.abc.Iterable,
		bytes
	)) and not isinstance(value, str)



class Datastore(trio.abc.AsyncResource):
	"""A Datastore represents storage for any string key to binary value pair.

	Datastores are general enough to be backed by all kinds of different storage:
	in-memory caches, databases, a remote datastore, flat files on disk, etc.
	
	The general idea is to wrap a more complicated storage facility in a simple,
	uniform interface, keeping the freedom of using the right tools for the job.
	In particular, a Datastore can aggregate other datastores in interesting ways,
	like sharded (to distribute load) or tiered access (caches before databases).
	
	While Datastores should be written general enough to accept all sorts of
	values, some implementations will undoubtedly have to be specific (e.g. SQL
	databases where fields should be decomposed into columns), particularly to
	support queries efficiently.
	"""
	
	__slots__ = ()

	# Some possibly useful types (assigned at the end of this file)
	ADAPTER_T:  type
	METADATA_T: type
	RECEIVE_T:  type
	
	_DS = typing.TypeVar("_DS", bound="Datastore")
	
	
	@classmethod
	@util.decorator.awaitable_to_context_manager
	async def create(cls: typing.Type[_DS], *args: typing.Any, **kwargs: typing.Any) -> _DS:
		return cls(*args, **kwargs)  # type: ignore[call-arg]
	
	# Main API. Datastore implementations MUST implement these methods.
	
	
	@abc.abstractmethod
	async def get(self, key: key_.Key) -> util.stream.ReceiveStream:
		"""Returns the data named by `key` or raises `KeyError` otherwise
		
		Important
		---------
		You **must** exhaust or manually close the returned iterable to ensure
		that possibly associated resources, like open file descriptors, are
		free'd.

		Arguments
		---------
		key
			Key naming the binary data to retrieve
		
		Raises
		------
		KeyError
			The given object was not present in this datastore
		RuntimeError
			An internal error occurred
		"""
		pass


	async def put(self, key: key_.Key, value: util.stream.ArbitraryReceiveStream) -> None:
		"""Stores or replaces the data named by `key` with `value`
		
		Arguments
		---------
		key
			Key naming the binary data slot to store at
		value
			A synchronous or asynchronous bytes or iterable of bytes object
			yielding the data to store
		
		Raises
		------
		RuntimeError
			An internal error occurred
		"""
		assert is_valid_value_type(value)
		await self._put(key, util.stream.receive_stream_from(value))
	

	@abc.abstractmethod
	async def _put(self, key: key_.Key, value: util.stream.ReceiveStream) -> None:
		"""Like :meth:`put`, but always receives a `datastore.util.ReceiveStream`
		   compatible object, so that your datastore implementation doesn't
		   have to do any conversion anymore
		"""
		pass
	
	
	@abc.abstractmethod
	async def delete(self, key: key_.Key) -> None:
		"""Removes the data named by `key`
		
		Arguments
		---------
		key
			Key naming the binary data slot to remove
		
		Raises
		------
		KeyError
			The given object was not present in this datastore
		RuntimeError
			An internal error occurred
		"""
		pass
	
	
	# Secondary API. Datastores MAY provide optimized implementations.
	
	
	async def contains(self, key: key_.Key) -> bool:
		"""Returns whether any data named by `key` exists
		
		The default implementation pays the cost of a get. Some datastore
		implementations may optimize this.
		
		Arguments
		---------
		key
			Key naming the object to check.
		"""
		try:
			await (await self.get(key)).aclose()
			return True
		except KeyError:
			return False
	
	
	async def get_all(self, key: key_.Key) -> bytes:
		"""Returns all the data named by `key` at once or raises `KeyError`
		   otherwise
		
		Arguments
		---------
		key
			Key naming the binary data to retrieve
		
		Raises
		------
		KeyError
			The given object was not present in this datastore
		RuntimeError
			An internal error occurred
		"""
		return await (await self.get(key)).collect()
	
	
	async def stat(self, key: key_.Key) -> util.metadata.StreamMetadata:
		"""Returns any metadata associated with the data stream named by `key`
		or raises `KeyError` otherwise
		
		Arguments
		---------
		key
			Key naming the data stream to query
		
		Raises
		------
		KeyError
			The given object was not present in this datastore
		RuntimeError
			An internal error occurred
		"""
		async with await self.get(key) as chann:
			return util.metadata.StreamMetadata(
				atime = chann.atime,
				mtime = chann.mtime,
				btime = chann.btime,
				size  = chann.size
			)
	
	
	def datastore_stats(self, selector: key_.Key = None, *, _seen: typing.Set[int] = None) \
	    -> util.metadata.DatastoreMetadata:
		"""Returns metadata of this datastore
		
		Unless overwritten this will not return any interesting value. In general,
		datastore backing implementations should try to at least expose a proper
		size measure if that is possible without any major accounting overhead.
		
		Arguments
		---------
		selector
			Used to select the backing store for some datastore adapters (such as
			mount) that have more than one backing store
			
			For datastore backends this will generally be ignored.
		_seen
			Set of Python object IDs of datastores already visited while gathering
			stats from datastore adapters with more than one then one backing store
			
			For datastore backends this must be silently ignored.
		
		Raises
		------
		RuntimeError
			An internal error occurred
		"""
		# The following should NOT be `util.metadata.DatastoreMetadata.IGNORE` as
		# that value would indicate that this datastore should be ignored during
		# size estimation rather than not implementing size estimation
		return util.metadata.DatastoreMetadata()
	
	
	async def aclose(self) -> None:
		"""Closes this any resources held by this datastore, possibly blocking
		
		Carefully read the documentation of :class:`trio.abc.AsyncResource`,
		particularily with regards to concellation and forceful closings, when
		implementating this.
		"""
		pass



class NullDatastore(Datastore):
	"""Stores nothing, but conforms to the API. Useful to test with."""
	
	__slots__ = ()

	async def get(self, key: key_.Key) -> util.stream.ReceiveStream:
		"""Unconditionally raise `KeyError`"""
		raise KeyError(key)

	async def _put(self, key: key_.Key, value: util.stream.ReceiveStream) -> None:
		"""Do nothing with `key` and ignore the `value`"""
		pass

	async def delete(self, key: key_.Key) -> None:
		"""Pretend there is any object that could be removed by the name `key`"""
		pass

	async def query(self, query: query_.Query) -> query_.Cursor:
		"""This won't ever match anything"""
		return query([])  # type: ignore[no-any-return]



class DictDatastore(Datastore):
	"""Simple straw-man in-memory datastore backed by nested dicts."""
	
	__slots__ = ("_items",)

	_items: typing.Dict[str, typing.Dict[key_.Key, bytes]]

	def __init__(self) -> None:
		self._items = {}
	
	
	def _collection(self, key: key_.Key) -> typing.Dict[key_.Key, bytes]:
		"""Returns the namespace collection for `key`."""
		collection = str(key.path)
		if collection not in self._items:
			self._items[collection] = dict()
		return self._items[collection]
	
	
	async def get(self, key: key_.Key) -> util.stream.ReceiveStream:
		"""Returns the object named by `key` or raises `KeyError`.
		
		Retrieves the object from the collection corresponding to ``key.path``.
		
		Arguments
		---------
		key
			Key naming the object to retrieve.
		"""
		return util.stream.receive_stream_from(self._collection(key)[key])
	
	
	async def get_all(self, key: key_.Key) -> bytes:
		"""Returns the object named by `key` or raises `KeyError`.
		
		Retrieves the object from the collection corresponding to ``key.path``.
		
		Arguments
		---------
		key
			Key naming the object to retrieve.
		"""
		return self._collection(key)[key]
	
	
	async def _put(self, key: key_.Key, value: util.stream.ReceiveStream) -> None:
		"""Stores the object `value` named by `key`.
		
		Stores the object in the collection corresponding to ``key.path``.
		
		Arguments
		---------
		key
			Key naming `value`
		value
			The object to store
		"""
		self._collection(key)[key] = await value.collect()
	
	
	async def delete(self, key: key_.Key) -> None:
		"""Removes the object named by `key` or raises `KeyError` if it did not
		   exist.
		
		Removes the object from the collection corresponding to ``key.path``.
		
		Arguments
		---------
		key
			Key naming the object to remove.
		"""
		del self._collection(key)[key]
		
		if len(self._collection(key)) == 0:
			del self._items[str(key.path)]
	
	
	async def contains(self, key: key_.Key) -> bool:
		"""Returns whether the object named by `key` exists.
		
		Checks for the object in the collection corresponding to ``key.path``.
		
		Arguments
		---------
		key
			Key naming the object to check.
		"""
		return key in self._collection(key)
	
	
	async def stat(self, key: key_.Key) -> util.metadata.StreamMetadata:
		"""Returns the length of the byte sequence named by `key` if it exists.
		
		Checks for the sequence in the collection corresponding to ``key.path``.
		
		Arguments
		---------
		key
			Key naming a byte sequence
		"""
		return util.metadata.StreamMetadata(size=len(self._collection(key)[key]))
	
	
	def datastore_stats(self, selector: key_.Key = None, *, _seen: typing.Set[int] = None) \
	    -> util.metadata.DatastoreMetadata:
		"""Returns the number of bytes stored in this datastore
		
		Arguments
		---------
		selector
			Ignored by backing datastores
		"""
		size = sum(map(lambda c: sum(map(len, c.values())), self._items.values()))
		return util.metadata.DatastoreMetadata(size=size, size_accuracy="exact")
	
	
	def __len__(self) -> int:
		return sum(map(len, self._items.values()))
	
	
	async def aclose(self) -> None:
		"""Deletes all items from this datastore"""
		self._items.clear()
		await super().aclose()



class Adapter(Datastore):
	"""Represents a non-concrete datastore that adds functionality between the
	   client and a lower-level datastore.
	
	Shim datastores do not actually store
	data themselves; instead, they delegate storage to an underlying child
	datastore. The default implementation just passes all calls to the child.
	"""
	__slots__ = ("child_datastore",)
	
	FORWARD_CONTAINS: bool = False
	FORWARD_GET_ALL:  bool = False
	FORWARD_STAT:     bool = False
	
	child_datastore: Datastore
	
	def __init__(self, datastore: Datastore):
		"""Initializes this DatastoreAdapter with child `datastore`."""
		self.child_datastore = datastore
	
	
	# default implementation just passes all calls to child
	
	
	async def get(self, key: key_.Key) -> util.stream.ReceiveStream:
		"""Returns the binary stream named by `key` or raises `KeyError` if
		   it does not exist.

		Default shim implementation simply returns ``child_datastore.get(key)``
		Override to provide different functionality, for example::

			async def get(self, key):
				# Collect the data returned by child and decode it as JSON
				# (Note: Use `datastore.serializer.json` rather than this for real apps.)
				value = await self.child_datastore.get_all(key)
				return datastore.util.receive_stream_from(json.loads(value))

		Arguments
		---------
		key
			Key naming the data to retrieve.
		"""
		return await self.child_datastore.get(key)
	
	
	async def _put(self, key: key_.Key, value: util.stream.ReceiveStream) -> None:
		"""Stores the data from the binary stream `value` at name `key`.
		
		Default shim implementation simply calls ``child_datastore.put(key, value)``
		Override to provide different functionality, for example::
		
			async def _put(self, key, value):
				value = json.dumps(await value.collect())
				await self.child_datastore.put(key, value)
		
		Arguments
		---------
		key
			Key naming `value`.
		value
			The data to store.
		"""
		await self.child_datastore.put(key, value)
	
	
	async def delete(self, key: key_.Key) -> None:
		"""Removes the object named by `key`.

		Default shim implementation simply calls ``child_datastore.delete(key)``
		Override to provide different functionality.

		Arguments
		---------
		key
			Key naming the data to remove.
		"""
		await self.child_datastore.delete(key)
	
	
	async def get_all(self, key: key_.Key) -> bytes:
		"""Returns the binary data named by `key` or raises `KeyError` if it
		   does not exist.
		
		Default shim implementation simply returns ``child_datastore.get_all(key)``
		if ``FORWARD_GET_ALL`` is `True`, ``(await get(key)).collect()`` otherwise.
		
		Override to provide different functionality, for example::
		
			async def get_all(self, key):
				# Collect the data returned by child and decode it as JSON
				# (Note: Use `datastore.serializer.json` rather than this for real apps.)
				value = await self.child_datastore.get_all(key)
				return datastore.util.receive_stream_from(json.loads(value))

		Arguments
		---------
		key
			Key naming the object to retrieve
		"""
		if self.FORWARD_GET_ALL:
			return await self.child_datastore.get_all(key)
		else:
			return await Datastore.get_all(self, key)
	
	
	async def contains(self, key: key_.Key) -> bool:
		"""Returns whether any data named by `key` exists
		
		Default shim implementation simply returns ``child_datastore.contains(key)``
		if ``FORWARD_CONTAINS`` is `True`, ``not (get(key) raises KeyError)`` otherwise.
		
		Arguments
		---------
		key
			Key naming the object to check.
		"""
		if self.FORWARD_CONTAINS:
			return await self.child_datastore.contains(key)
		else:
			return await Datastore.contains(self, key)
	
	
	async def stat(self, key: key_.Key) -> util.metadata.StreamMetadata:
		"""Returns the metadata of the stream named by `key` if it exists
		
		Default shim implementation simply returns ``child_datastore.stat(key)``
		if ``FORWARD_STAT`` is `True`, ``get(key)`` otherwise.
		
		Arguments
		---------
		key
			Key naming the stream to check.
		"""
		if self.FORWARD_STAT:
			return await self.child_datastore.stat(key)
		else:
			return await Datastore.stat(self, key)
	
	
	def datastore_stats(self, selector: key_.Key = None, *, _seen: typing.Set[int] = None) \
	    -> util.metadata.DatastoreMetadata:
		"""Returns metadata of the child datastore
		
		Arguments
		---------
		selector
			Used to select the backing store for some datastore adapters (such as
			mount) that have more than one backing store
			
			If this is ``None``, the result will be the sum of all datastores
			attached to this adapter.
		_seen
			Set of Python object IDs of datastores already visited while gathering
			stats from datastore adapters with more than one then one backing store
			
			This is required to ensure that no backing datastore is counted more
			than once if `selector` is ``None``.
		
		Raises
		------
		RuntimeError
			An internal error occurred in the child datastore
		"""
		_seen = _seen if _seen is not None else set()
		
		if id(self.child_datastore) in _seen:
			return util.metadata.DatastoreMetadata.IGNORE
		
		_seen.add(id(self.child_datastore))
		return self.child_datastore.datastore_stats(selector, _seen=_seen)
	
	
	async def aclose(self) -> None:
		"""Closes this any resources held by the child datastore
		
		Carefully read the documentation of :class:`trio.abc.AsyncResource`,
		particularily with regards to concellation and forceful closings, when
		implementating this.
		"""
		try:
			await self.child_datastore.aclose()
		finally:
			await super().aclose()


Datastore.ADAPTER_T  = Adapter
Datastore.METADATA_T = util.metadata.StreamMetadata
Datastore.RECEIVE_T  = util.stream.ReceiveStream


"""

Hello Tiered Access

	>>> import pymongo
	>>> import datastore.core
	>>>
	>>> from datastore.impl.mongo import MongoDatastore
	>>> from datastore.impl.lrucache import LRUCache
	>>> from datastore.impl.filesystem import FileSystemDatastore
	>>>
	>>> conn = pymongo.Connection()
	>>> mongo = MongoDatastore(conn.test_db)
	>>>
	>>> cache = LRUCache(1000)
	>>> fs = FileSystemDatastore('/tmp/.test_db')
	>>>
	>>> ds = datastore.TieredDatastore([cache, mongo, fs])
	>>>
	>>> hello = datastore.Key('hello')
	>>> ds.put(hello, 'world')
	>>> ds.contains(hello)
	True
	>>> ds.get(hello)
	'world'
	>>> ds.delete(hello)
	>>> ds.get(hello)
	None

Hello Sharding

	>>> import datastore.core
	>>>
	>>> shards = [datastore.DictDatastore() for i in range(0, 10)]
	>>>
	>>> ds = datastore.ShardedDatastore(shards)
	>>>
	>>> hello = datastore.Key('hello')
	>>> ds.put(hello, 'world')
	>>> ds.contains(hello)
	True
	>>> ds.get(hello)
	'world'
	>>> ds.delete(hello)
	>>> ds.get(hello)
	None
"""
