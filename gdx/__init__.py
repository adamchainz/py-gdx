from itertools import cycle

import numpy
import pandas
import xray

import gdxcc

from .api import type_str, vartype_str, GDX

from logging import debug, info
# commented: for debugging
# import logging
# logging.basicConfig(level=logging.DEBUG)


__version__ = '2'


__all__ = [
    'File',
    ]


class File(xray.Dataset):
    """Load the file at *filename* into memory.

    *mode* must be 'r' (writing GDX files is not currently supported). If
    *lazy* is ``True`` (default), then the data for GDX parameters is not
    loaded until each parameter is first accessed.

    """
    # For the benefit of xray.Dataset.__getattr__
    _api = None
    _index = []
    _state = {}
    _alias = {}

    def __init__(self, filename='', lazy=True, skip=set()):
        """Constructor."""
        super().__init__()  # Invoke Dataset constructor

        # load the GDX API
        self._api = GDX()
        self._api.open_read(filename)

        # Basic information about the GDX file
        v, p = self._api.file_version()
        sc, ec = self._api.system_info()
        self.attrs['version'] = v.strip()
        self.attrs['producer'] = p.strip()
        self.attrs['symbol_count'] = sc
        self.attrs['element_count'] = ec

        # Initialize private variables
        self._index = [None for _ in range(sc + 1)]
        self._state = {}
        self._alias = {}

        # Read symbols
        for s_num in range(sc + 1):
            name, type_code = self._load_symbol(s_num)
            if type_code == gdxcc.GMS_DT_SET and name not in skip:
                self._load_symbol_data(name)

        if not lazy:
            for name in filter(None, self._index):
                if name not in skip:
                    self._load_symbol_data(name)

    def _load_symbol(self, index):
        """Load the *index*-th Symbol in the GDX file."""
        # Load basic information
        name, dim, type_code = self._api.symbol_info(index)
        n_records, vartype, desc = self._api.symbol_info_x(index)

        self._index[index] = name  # Record the name

        attrs = {
            'index': index,
            'name': name,
            'dim': dim,
            'type_code': type_code,
            'records': n_records,
            'vartype': vartype,
            'description': desc,
            }

        # Assemble a string description of the Symbol's type
        type_str_ = type_str[type_code]
        if type_code == gdxcc.GMS_DT_PAR and dim == 0:
            type_str_ = 'scalar'
        try:
            vartype_str_ = vartype_str[vartype]
        except KeyError:
            vartype_str_ = ''
        attrs['type_str'] = '{} {}'.format(vartype_str_, type_str_)

        debug('Loading #{index} {name}: {dim}-D, {records} records, '
              '"{description}"'.format(**attrs))

        # Equations and Aliases require limited processing
        if type_code == gdxcc.GMS_DT_EQU:
            info('Loading of GMS_DT_EQU not implemented: {} {} not loaded.'.
                 format(index, name))
            self._state[name] = None
            return name, type_code
        elif type_code == gdxcc.GMS_DT_ALIAS:
            parent = desc.replace('Aliased with ', '')
            self._alias[name] = parent
            if self[parent].attrs['_gdx_type_code'] == gdxcc.GMS_DT_SET:
                # Duplicate the variable
                self._variables[name] = self._variables[parent]
                self._state[name] = True
                super().set_coords(name, inplace=True)
            else:
                raise NotImplementedError('Cannot handle aliases of symbols '
                                          'except GMS_DT_SET: {} {} not loaded'
                                          .format(index, name))
            return name, type_code

        # The Symbol is either a Set, Parameter or Variable
        try:  # Read the domain, as a list of names
            domain = self._api.symbol_get_domain_x(index)
            debug('domain: {}'.format(domain))
        except Exception:  # gdxSymbolGetDomainX fails for the universal set
            assert name == '*'
            domain = []

        # Cache the attributes
        attrs['domain'] = domain
        self._state[name] = {'attrs': attrs}

        return name, type_code

    def _load_symbol_data(self, name):
        """Load the Symbol *name*."""
        if self._state[name] in (True, None):  # Skip Symbols already loaded
            return

        # Unpack attributes
        attrs = self._state[name]['attrs']
        index, dim, domain, records = [attrs[k] for k in ('index', 'dim',
                                                          'domain', 'records')]

        # Read the data
        self._cache_data(name, index, dim, records)

        # If the GAMS method 'sameas' is invoked in a program, the resulting
        # GDX file contains an empty Set named 'SameAs' with domain (*,*). Do
        # not read this
        if name == 'SameAs' and domain == ['*', '*'] and records == 0:
            self._state[name] = None
            self._index[index] = None
            return

        domain = self._infer_domain(name, domain,
                                    self._state[name]['elements'])

        # Create an xray.DataArray with the Symbol's data
        self._add_symbol(name, dim, domain, attrs)

    def _cache_data(self, name, index, dim, records):
        """Read data for the Symbol *name* from the GDX file."""
        # Initiate the data read. The API method returns a number of records,
        # which should match that given by gdxSymbolInfoX in _load_symbol()
        records2 = self._api.data_read_str_start(index)
        assert records == records2, \
            ('{}: gdxSymbolInfoX ({}) and gdxDataReadStrStart ({}) disagree on'
             ' number of records.').format(name, records, records2)

        # Indices of data records, one list per dimension
        elements = [list() for _ in range(dim)]
        # Data points. Keys are index tuples, values are data. For a 1-D Set,
        # the data is the GDX 'string number' of the text associated with the
        # element
        data = {}
        try:
            while True:  # Loop over all records
                labels, value, _ = self._api.data_read_str()  # Next record
                # Update elements with the indices
                for j, label in enumerate(labels):
                    if label not in elements[j]:
                        elements[j].append(label)
                # Convert a 1-D index from a tuple to a bare string
                key = labels[0] if dim == 1 else tuple(labels)
                # The value is a sequence, containing the level, marginal,
                # lower & upper bounds, etc. Store only the value (first
                # element).
                data[key] = value[gdxcc.GMS_VAL_LEVEL]
        except Exception:
            if len(data) == records:
                pass  # All data has been read
            else:
                raise  # Some other read error

        # Cache the read data
        self._state[name].update({
            'data': data,
            'elements': elements,
            })

    def _infer_domain(self, name, domain, elements):
        """Infer the domain of the Symbol *name*.

        Lazy GAMS modellers may create variables like myvar(*,*,*,*). If the
        size of the universal set * is large, then attempting to instantiate
        a xray.DataArray with this many elements may cause a MemoryError. For
        every dimenions of *name* defined on the domain '*' this method tries
        to find a Set from the file which contains all the labels appearing in
        *name*'s data.

        """
        if '*' not in domain:
            return domain
        debug('guessing a better domain for {}: {}'.format(name, domain))

        # Domain as a list of references to Variables in the File/xray.Dataset
        domain_ = [self[d] for d in domain]

        for i, d in enumerate(domain_):  # Iterate over dimensions
            e = set(elements[i])
            if d.name != '*' or len(e) == 0:
                assert set(d.values).issuperset(e)
                continue  # The stated domain matches the data; or no data
            # '*' is given, try to find a smaller domain for this dimension
            for s in self.coords.values():  # Iterate over every Set/Coordinate
                if s.ndim == 1 and set(s.values).issuperset(e) and \
                        len(s) < len(d):
                    d = s  # Found a smaller Set; use this instead
            domain_[i] = d

        # Convert the references to names
        inferred = [d.name for d in domain_]

        if domain != inferred:
            # Store the result
            self._state[name]['attrs']['domain_inferred'] = inferred
            debug('…inferred {}.'.format(inferred))
        else:
            debug('…failed.')

        return inferred

    def _root_dim(self, dim):
        """Return the ultimate ancestor of the 1-D Set *dim*."""
        parent = self[dim].dims[0]
        return dim if parent == dim else self._root_dim(parent)

    def _empty(self, *dims, **kwargs):
        """Return an empty numpy.ndarray for a GAMS Set or Parameter."""
        size = []
        dtypes = []
        for d in dims:
            size.append(len(self[d]))
            dtypes.append(self[d].dtype)
        dtype = kwargs.pop('dtype', numpy.result_type(*dtypes))
        fv = kwargs.pop('fill_value')
        return numpy.full(size, fill_value=fv, dtype=dtype)

    def _add_symbol(self, name, dim, domain, attrs):
        """Add a xray.DataArray with the data from Symbol *name*."""
        # Transform the attrs for storage, unpack data
        gdx_attrs = {'_gdx_{}'.format(k): v for k, v in attrs.items()}
        data = self._state[name]['data']
        elements = self._state[name]['elements']

        # Erase the cache; this also prevents __getitem__ from triggering lazy-
        # loading, which is still in progress
        self._state[name] = True

        kwargs = {}  # Arguments to xray.Dataset.__setitem__()
        if dim == 0:
            # 0-D Variable or scalar Parameter
            super().__setitem__(name, ([], data.popitem()[1], gdx_attrs))
            return
        elif attrs['type_code'] == gdxcc.GMS_DT_SET:  # GAMS Set
            if dim == 1:
                if (domain == ['*'] or domain == [] or domain == [name]):
                    # One-dimensional, 'top-level' Set
                    self.coords[name] = elements[0]
                    self.coords[name].attrs = gdx_attrs
                    return
                # Some subset; fill empty elements with the empty string
                kwargs['fill_value'] = ''
            else:
                # Multi-dimensional Sets are mappings indexed by other Sets;
                # elements are either 'on'/True or 'off'/False
                kwargs['dtype'] = bool
                kwargs['fill_value'] = False

            # Don't define over the actual domain dimensions, but over the
            # parent Set/xray.Coordinates for each dimension
            dims = [self._root_dim(d) for d in domain]

            # Update coords
            self.coords.__setitem__(name, (dims, self._empty(*domain,
                                                             **kwargs),
                                           gdx_attrs))

            # Store the elements
            for k in data.keys():
                self[name].loc[k] = k if dim == 1 else True
        else:  # 1+-dimensional GAMS Parameters
            kwargs['dtype'] = float
            kwargs['fill_value'] = numpy.nan

            dims = [self._root_dim(d) for d in domain]  # Same as above

            # Create an empty xray.DataArray; this ensures that the data
            # read in below has the proper form and indices
            super().__setitem__(name, (dims, self._empty(*domain, **kwargs),
                                gdx_attrs))

            # Fill in extra keys
            longest = numpy.argmax(self[name].values.shape)
            iters = []
            for i, d in enumerate(dims):
                if i == longest:
                    iters.append(self[d].to_index())
                else:
                    iters.append(cycle(self[d].to_index()))
            data.update({k: numpy.nan for k in set(zip(*iters)) -
                         set(data.keys())})

            # Use pandas and xray IO methods to convert data, a dict, to a
            # xray.DataArray of the correct shape, then extract its values
            tmp = pandas.Series(data)
            tmp.index.names = dims
            tmp = xray.DataArray.from_series(tmp).reindex_like(self[name])
            self[name].values = tmp.values

    def dealias(self, name):
        """Identify the GDX Symbol that *name* refers to, and return the
        corresponding :class:`xray.DataArray`."""
        return self[self._alias[name]] if name in self._alias else self[name]

    def extract(self, name):
        """Extract the GAMS Symbol *name* from the dataset.

        The Sets and Parameters in the :class:`File` can be accessed directly,
        as e.g. `f['name']`; but for more complex xray operations, such as
        concatenation and merging, this carries along sub-Sets and other
        Coordinates which confound xray.

        :func:`extract()` returns a self-contained xray.DataArray with the
        declared dimensions of the Symbol (and *only* those dimensions), which
        does not make reference to the :class:`File`.
        """
        # Trigger lazy-loading if needed
        self._load_symbol_data(name)

        result = self[name].copy()

        # Declared dimensions of the Symbol, and their parents
        dims = {c: self._root_dim(c) for c in result.attrs['_gdx_domain']}
        keep = set(dims.keys()) | set(dims.values())

        # Drop extraneous dimensions
        for c in set(result.coords) - keep:
            del result[c]

        # Reduce the data
        for c, p in dims.items():
            if c == '*':  # Dimension is '*', drop empty labels
                result = result.dropna(dim='*', how='all')
            elif c == p:
                continue
            else:
                # Dimension is indexed by 'p', but declared 'c'. First drop
                # the elements which do not appear in the sub-Set c;, then
                # rename 'p' to 'c'
                drop = set(self[p].values) - set(self[c].values) - set('')
                result = result.drop(drop, dim=p).rename({p: c})
        return result

    def info(self, name):
        """Informal string representation of a Symbol."""
        if isinstance(self._state[name], dict):
            attrs = self._state[name]['attrs']
            return '{} {}({}) — {} records: {}'.format(
                attrs['type_str'], name, ','.join(attrs['domain']),
                attrs['records'], attrs['description'])
        else:
            print(self[name])

    def _loaded_and_cached(self, type_code):
        """Return a list of loaded and not-loaded Symbols of *type_code*."""
        names = set()
        for name, state in self._state.items():
            if state is True:
                tc = self._variables[name].attrs['_gdx_type_code']
            elif isinstance(state, dict):
                tc = state['attrs']['type_code']
            else:
                continue
            if tc == type_code:
                names.add(name)
        return names

    def set(self, name):
        """Return the elements of GAMS Set *name*.

        Because of the need to store non-null labels for each element of a
        Coordinate, a GAMS sub-Set will contain some '' elements, corresponding
        to elements of the parent Set which do not appear in *name*.
        :func:`set()` returns the elements, absent these placeholders.

        """
        return [k for k in self[name].to_index() if k != '']

    def sets(self):
        """Return a list of all GDX Sets."""
        return self._loaded_and_cached(gdxcc.GMS_DT_SET)

    def parameters(self):
        """Return a list of all GDX Parameters."""
        return self._loaded_and_cached(gdxcc.GMS_DT_PAR)

    def get_symbol_by_index(self, index):
        """Retrieve the GAMS Symbol from the *index*-th position of the
        :class:`File`."""
        return self[self._index[index]]

    def __getitem__(self, key):
        """Set element access."""
        try:
            return super().__getitem__(key)
        except KeyError:
            if isinstance(self._state[key], dict):
                debug('Lazy-loading {}'.format(key))
                self._load_symbol_data(key)
                return super().__getitem__(key)
            else:
                raise
