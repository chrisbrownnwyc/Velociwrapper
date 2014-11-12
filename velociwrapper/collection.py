from elasticsearch import Elasticsearch, NotFoundError,helpers
from datetime import date,datetime
from dateutil import parser
from uuid import uuid4
import json
import types
import copy
import logging
from .config import es,dsn,default_index,bulk_chunk_size,results_per_page, logger
from .es_types import *

# Raised when no results are found for one()
class NoResultsFound(Exception):
	pass

# raised if the body of a search in _build_body() gets unexpected conditions
class MalformedBodyError(Exception):
	pass

class VWCollection(object):
	def __init__(self,items=[],**kwargs):
		self.bulk_chunk_size = bulk_chunk_size

		self.bulk_chunk_size = kwargs.get('bulk_chunk_size', bulk_chunk_size)

		self._sort = []
		
		self.results_per_page = kwargs.get('results_per_page', results_per_page)


		if kwargs.get('base_obj'):
			self.base_obj = kwargs.get('base_obj')
		else:
			try:
				self.base_obj = self.__class__.__model__
			except AttributeError:
				raise AttributeError('Base object must contain a model or pass base_obj')

		self._es = es

		if '__index__' in dir(self.base_obj):
			idx = self.base_obj.__index__
		else:
			idx = default_index

		self._search_params = []
		self._raw = {}
		self.idx = idx
		self.type = self.base_obj.__type__
		self._special_body = {}
		self._items = items # special list of items that can be committed in bulk

		# these values are used in the _build_body() to determine where additional _build_body()
		# options should exist. Defaults to and/must
		self._last_top_level_boolean = None
		self._last_boolean = None

	def _create_obj_list(self,es_rows):
		retlist = []
		for doc in es_rows:
			if doc.get('_source'):
				retlist.append( self._create_obj(doc) )

		return retlist

	def _create_obj(self,doc):
		src = doc.get('_source')
		src['_set_by_query'] = True
		src['id'] = doc.get('_id')
		return self.base_obj(**src)

	def search(self,q):
		self._search_params.append(q)
		return self

	# setup a raw request
	def raw(self, raw_request):
		self._raw = raw_request
		return self

	def _do_search(self,q):
		results = self._es.search(index=self.idx,q=q,doc_type=self.type)
		return self._create_obj_list( results.get('hits').get('hits') )

	def filter_by( self, condition = 'and',**kwargs ):
		groups = []
		for k,v in kwargs.iteritems():
			if k == 'id' or k == 'ids':
				id_filter = v
				if not isinstance(id_filter, list ):
					id_filter = [id_filter]

				self._build_body( filter={"ids": {"values": id_filter } } )
			else:
				search_value = ''
				if isinstance(v, list):
					# lists are treat as like "OR"
					search_value = " or ".join( [ unicode(vitem) for vitem in v] )
					search_value = "(" + search_value + ")"
				else:
					search_value = unicode(v)

				groups.append( unicode(k) + ':"' + search_value + '"')

		conditions = {
			'and': self.and_,
			'or': self.or_
		}
		
		# if everything was by ID there may be no groups
		if groups:
			query = conditions[condition.lower()](*groups)

			#if condition == 'and':
			#	query = self.and_(*groups)
			
			return self.search( query )
		else:
			return self

	def exact( self, field, value ):
		try:
			field_template = getattr( self.base_obj, field)

			if type(field_template) != ESType:
				field_template = create_es_type( field_template )

			for estype in [String,IP,Attachment]:
				if isinstance( field_template, estype ) and field_template.analyzed == True:
					logger.warn( str(estype.__class__.__name__) + ' types may not exact match correctly if they are analyzed' )

		except AttributeError:
			logger.warn( str(field) + ' is not in the base model.' )
		
		if isinstance(value, list):
			self._build_body( filter={"terms": { field: value } } )
		else:
			self._build_body( filter={"term": { field: value } } )

		return self
		

	def or_(self,*args):
		return ' OR '.join(args)

	def and_(self,*args):
		return ' AND '.join(args)

	def get(self,id):
		try:
			return self._create_obj( self._es.get(index=self.idx,doc_type=self.type,id=id) )
		except:
			return None

	def get_in(self, ids):
		
		if len(ids) > 0: # check for ids. empty list returns an empty list (instead of exception)
			res = self._es.mget(index=self.idx,doc_type=self.type,body={'ids':ids})
			if res and res.get('docs'):
				return self._create_obj_list( res.get('docs') )

		return []

	def get_like_this(self,doc_id):
		res = self._es.mlt(index=self.idx,doc_type=self.type,id=doc_id )
		if res and res.get('docs'):
			return self._create_obj_list( res.get('docs') )
		else:
			return []

	def sort(self, **kwargs ):
		for k,v in kwargs.iteritems():
			v = v.lower()
			if v not in ['asc','desc']:
				v = 'asc'

			self._sort.append( '%s:%s' % (k,v) )
		return self

	def clear_previous_search( self ):
		self._raw = {}
		self._search_params = []
		self._special_body = {}

	def _create_search_params( self ):
		q = {
			'index': self.idx,
			'doc_type': self.type
		}

		if self._raw:
			q['body'] = self._raw
		elif len(self._search_params) > 0:
			q['q'] = self.and_(*self._search_params)

		else:
			q['body'] = {'query':{'match_all':{} } }
		

		# this is the newer search by QDSL
		if self._special_body:
			q['body'] = self._special_body
		
		logger.debug(json.dumps(q))
		return q


	def count(self):
		params = self._create_search_params()
		resp = self._es.count(**params)
		return resp.get('count')


	def __len__(self):
		return self.count()

	def all(self,**kwargs):

		params = self._create_search_params()
		if not params.get('size'):
			params['size'] = self.results_per_page

		if kwargs.get('results_per_page') != None:
			kwargs['size'] = kwargs.get('results_per_page')
			del kwargs['results_per_page']

		if kwargs.get('start') != None:
			kwargs['from_'] = kwargs.get('start')
			del kwargs['start']
		
		logger.debug( json.dumps( self._sort )  )

		params.update(kwargs)
		if len(self._sort) > 0:
			if params.get('sort') and isinstance(params['sort'], list):
				params['sort'].extend(self._sort)
			else:
				params['sort'] = self._sort
		
		if params.get('sort'):
			if isinstance(params['sort'], list):
				params['sort'] = ','.join(params.get('sort'))
			else:
				raise TypeError('"sort" argument must be a list')
		
		results = self._es.search( **params )
		rows = results.get('hits').get('hits')

		return self._create_obj_list( rows )

	def one(self,**kwargs):
		results = self.all(results_per_page=1)
		try:
			return results[0]
		except IndexError:
			raise NoResultsFound('No result found for one()')

	# builds query bodies
	def _build_body( self, **kwargs ):
		

		_bool_condition = kwargs.get('condition')

		bool_set_default = False
		if not _bool_condition:
			if self._last_boolean:
				_bool_condition = self._last_boolean
			else:
				_bool_condition = 'must'
				bool_set_default = True

		try:
			del kwargs['condition']
		except KeyError:
			pass

		# translate standard booleans
		# and = must
		# not = must_not
		# or = should with minimum_should_match=1 (1 is default. You can still set it)

		_secondary_bool = None 	# this is when explicits are set, the secondary needs to be set as well if bool is explicit

		if _bool_condition == 'and':
			_bool_condition = 'must'
		elif _bool_condition = 'or':
			_bool_condition = 'should'
		elif _bool_condition = 'not':
			_bool_condition = 'must_not'

		# this is for things like geo_distance where we explicitly want the true and/or/not
		elif _bool_condition = 'explicit_and':
			_bool_condition = 'and'
			_secondary_bool = 'must'
		elif _bool_condition = 'explicit_or':
			_bool_condition = 'or'
			_secondary_bool = 'should'
		elif _bool_condition = 'explicit_not':
			_bool_condition = 'not'
			_secondary_bool = 'must_not'

		_minimum_should_match = 1
		if kwargs.get('minimum_should_match'):
			_minimum_should_match = kwargs.get('minimum_should_match')
			del kwargs['minimum_should_match']

		_with_explicit = None
		if kwargs.get('with_explicit'):
			_with_explicit = kwargs.get('with_explicit').lower()
			del kwargs['with_explicit']

		if not self._special_body:
			self._special_body = { "query": {} }
		
		if kwargs.get('filter'):
			if not self._special_body.get('query').get('filtered'):
				current_q = self._special_body.get('query')

				self._special_body['query']['filtered'] = { 'query': current_q, 'filter':{}  }

			#self._special_body['query']['filtered']['filter'].update( kwargs.get('filter'))
			
			# do we have any top level and/or/not filters already specified?
			_filter = self._special_body.get('query').get('filtered').get('filter')
			has_conditions = next( cond for cond in ['and','or','not'] if cond in _filter )
			
			if has_conditions:

				if _bool_condition in ['and','or','not']:
					if _bool_condition in _filter:
						if _filter[_bool_condition]:
							if not isinstance(_filter.get(_bool_condition, list) ):
								self._special_body['query']['filtered']['filter'][_bool_condition] = [_filter[_bool_condition]]

							self._special_body['query']['filtered']['filter'][_bool_condition].append( kwargs.get('filter') )
							_filter = self._special_body['query']['filtered']['filter']

					elif _filter:
						self._special_body['query']['filtered']['filter'] = { _bool_condition: [_filter] }
						_filter = self._special_body.get('query').get('filtered').get('filter')
						_filter.append( kwargs.get('filter') )
						
					else:
						self._special_body['query']['filtered']['filter'] = kwargs.get('filter')

					self._last_top_level_boolean = _bool_condition # for subsequent calls

				else:
					# we have to find the bool
						
					# if we have top level conditions then bool must appear inside one of them
					# 1. Try to use the specified position with_explicit=value. If it doesn't exist then fall through (and warn)
					# 2. Use the last precidence specified (if it was specified)
					# 3. try in the order of AND, OR, NOT
					# 4. If none have bools, add the bool to the first condition that exists (in the order of 'and','or','not') (using has_conditions whcih will be set to teh appropriate value)
					
					set_on_condition = False
					if _with_explicit and _filter.get(_with_explicit):
						set_on_condition = _with_explicit
						
						# set the top level bool to the explicit setting
						self._last_top_level_boolean = _with_explicit

					elif self._last_top_level_boolean and _filter.get(_last_top_level_boolean):
						set_on_condition = _last_top_level_boolean
					else:
						set_on_condition = next( cond for cond in ['and','or','not'] if _filter.get(cond) and _filter.get(cond).get('bool') )

					# if its still not set:
					if not set_on_condition:
						set_on_condition = has_conditions # has_conditions will be set to the first existing condition in order of ops
				
					if set_on_condition in _filter:
						if _filter.get( set_on_condition ).get('bool'):
							if _filter.get(set_on_condition).get('bool').get(_bool_condition):
								if isinstance(_filter.get(set_on_condition).get('bool').get(_bool_condition), list):
									_filter.get(set_on_condition).get('bool').get(_bool_condition).append(kwargs.get('filter'))
								else:
									current_filter = _filter.get(set_on_condition).get('bool').get(_bool_condition)
									self._special_body['query']['filtered']['filter']['bool'][_bool_condition] = [current_filter, kwargs.get('filter')]
									_filter = self._special_body['query']['filtered']['filter']
							else:
								_filter.get(set_on_condition).get('bool')[_bool_condition] = kwargs.get('filter')


						else:
							current_filter = _filter.get(set_on_condition)
							self._special_body['query']['filter']['filtered'][set_on_condition] = {'bool': { _bool_condition: current_filter } }
							_filter = self._special_body['query']['filter']['filtered']

							_filter[set_on_condition]['bool'][_bool_condition] = kwargs.get('filter')

						if 'bool' in _filter[set_on_condition]:
							if _minimum_should_match != 1:
								_filter[set_on_condition]['bool']['minimum_should_match'] = _minimum_should_match

							if kwargs.get('boost'):
								_filter[set_on_condition]['bool']['boost'] = kwargs.get('boost')
					else:
						# this should never happen
						raise MalformedBodyError


			else:
				# this is a normal boolean request
				if _filter.get('bool'):
					if _filter.get('bool').get(_bool_condition):
						if isinstance(_filter.get('bool').get(_bool_condition), list ):
							_filter.get('bool').get(_bool_condition).append( kwargs.get('filter') )
						else:
							current_filter = _filter.get('bool').get(_bool_condition)
							self._special_body['query']['filter']['filtered']['bool'][_bool_condition] = [current_filter, kwargs.get('filter')]
					else:
						_filter['bool'][_bool_condition] = kwargs.get('filter')

				elif _filter:
					current_filter = _filter
					self._special_body['query']['filtered']['filter'] = {'bool': { _bool_condition: [current_filter, kwargs.get('filter')] } }
					_filter = self._special_body['query']['filtered']['filter']
				else:
					self._special_body['query']['filtered']['filter'] = kwargs.get('filter')
					_filter = self._special_body['query']['filtered']['filter']


				if 'bool' in _filter:
					if _minimum_should_match != 1:
						_filter['bool']['minimum_should_match'] = _minimum_should_match

					if kwargs.get('boost'):
						_filter['bool']['boost'] = kwargs.get('boost')


		elif kwargs.get('query'):
			if 'filtered' in self._special_body.get('query'):
				query = self._special_body.get('filtered').get('filter')

			else:
				query = self._special_body

			if query.get('query'):
				if 'bool' in query.get('query'):
					if _bool_condition in query.get('query').get('bool'):
						if isinstance(query.get('query').get('bool').get(_bool_condition), list):
							query.get('query').get('bool').get(_bool_condition).append( kwargs.get('query') )
						else:
							current_q = query.get('query').get('bool').get(_bool_condition)

							query['query']['bool'][_bool_conditon] = [current_q, kwargs.get('query')]
					else:
						query['query']['bool'][_bool_condition] = kwargs.get('filter')

				else:
					current_q = query['query']
					query['query'] = {'bool': { _bool_condition: [current_q, kwargs.get('query')] } }

			else:
				query['query'] = kwargs.get('filter')
	
			if 'bool' in query.get('query'):
				if _minimum_should_match != 1:
					query['query']['bool']['minimum_should_match'] = _minimum_should_match

				if kwargs.get('boost'):
					query['query']['bool']['boost'] = kwargs.get('boost')

		

	def _special_body_is_filtered(self):
		return (self._special_body and self._special_body.get('query').get('filtered'))

	def range(self, field, **kwargs):
		q = {'range': { field: kwargs } }
		if self._special_body_is_filtered():
			d = {'filter': q }
		else:
			d = {'query': q }

		self._build_body(**d)
		return self

	def search_geo(self, field, distance, lat, lon):
		return self.raw( {
			"query": {
				"filtered": { 
					"query": {
						"match_all": {}
					},
					"filter": {
						"geo_distance": {
							"distance": distance,
							field: [ lon, lat ]
						}
					}
				}
			}
		})

	
	def delete(self, **kwargs):
		params = self._create_search_params()
		params.update(kwargs)
		self._es.delete_by_query( **params )


	def delete_in(self, ids):
		if not isinstance(ids, list):
			raise TypeError('argument to delete in must be a list.')

		bulk_docs = []
		for i in ids:
			this_id = i
			this_type = self.base_obj.__type__
			this_idx = self.idx
			if isinstance(i, VWBase):
				this_id = i.id
				this_type = i.__type__
				try:
					this_idx = i.__index__
				except AttributeError:
					pass

			bulk_docs.append( {'_op_type': 'delete', '_type': this_type, '_index': this_idx, '_id': this_id } )

		return helpers.bulk( self._es, bulk_docs, chunk_size=self.bulk_chunk_size)
	
	# commits items in bulk
	def commit(self, callback=None):
		bulk_docs = []

		if callback:
			if not callable(callback):
				raise TypeError('Argument 2 to commit() must be callable')

		for i in self._items:
			if callback:
				i = callback(i)

			this_dict = {}
			this_id = ''
			this_idx = self.idx
			this_type = self.base_obj.__type__
			if isinstance(i, VWBase):
				this_dict = i._create_source_document()
				this_type = i.__type__
				this_id = i.id
				try:
					this_idx = i.__index__
				except AttributeError:
					pass

			elif isinstance(i,dict):
				this_dict = i
				this_id = i.get('id')
			
			else:
				raise TypeError( 'Elments passed to the collection must be type of "dict" or "VWBase"' )
			
			if not this_id:
				this_id = str(uuid4())

			bulk_docs.append( {'_op_type': 'index', '_type': this_type, '_index': this_idx, '_id': this_id, '_source': this_dict } )

		return helpers.bulk(self._es,bulk_docs,chunk_size=self.bulk_chunk_size)
