# -*- coding: utf-8 -*-
"""Optional editable SQL data objects.

Ordinary SQL objects are read-only with respect to record editing: they expose
rows, records and statements, but there is no way to take a modified record and
write the changed values back to the row it came from.

This module adds an *opt-in* editable layer on top of `SQLTable`. An editable
object:

* exposes only the selected public layout – the identity column (primary key)
  is hidden and never appears in ``fields``, ``rows()`` or ``records()``;
* keeps the hidden identity and snapshot columns through native row filters,
  sorting and ``field_filter`` (including renames), because every derived
  statement is built on top of the previous one and hidden columns are restored
  whenever an operation only selects, renames or aliases columns;
* yields :class:`EditableRecord` objects from ``records()`` that remember the
  original physical values and the hidden row identity;
* on submit updates only the physical columns that have actually changed, on
  the corresponding original row, using an optimistic
  ``WHERE key = ? AND <all snapshot columns match>`` predicate. No match means
  the row was modified or deleted externally and is reported as
  :class:`EditConflictError`;
* applies the whole submitted batch in a single transaction, so if any single
  update fails nothing is written;
* refuses editing of results that can not be uniquely written back
  (aggregation, join, distinct, union, ...) with :class:`NotEditableError`.
  Such results are still readable, but their records are read-only.

Usage::

    table = store.get_object("items")
    editable = table.as_editable()          # ordinary reads are unchanged

    sel = ctx.op.filter_by_value(editable, "status", "new")
    sel = ctx.op.field_filter(sel, keep=["name", "amount"],
                              rename={"amount": "total"})
    sel = ctx.op.sort(sel, [("total", "desc")])

    records = list(sel.records())
    records[0]["total"] = 120
    sel.commit(records)                     # atomic, conflict-checked
"""

from ...errors import (ArgumentError, DataObjectError, NotEditableError,
                       EditConflictError, IsNotTargetError)
from ...metadata import FieldList
from .objects import SQLDataObject

try:
    import sqlalchemy
    from sqlalchemy import sql
    from sqlalchemy.engine import Engine
    from sqlalchemy.sql import select
    from sqlalchemy.sql.schema import Column as SAColumn
    from sqlalchemy.sql.selectable import Alias, CompoundSelect, Join, Select
    from sqlalchemy.sql.elements import Label
except ImportError:
    from ...common import MissingPackage
    sqlalchemy = MissingPackage("sqlalchemy", "SQL streams",
                                "http://www.sqlalchemy.org/")
    sql = sqlalchemy

__all__ = (
    "EditableSQLTable",
    "EditableSQLStatement",
    "EditableRecord",
)

# Labels of hidden columns. They must not clash with public field names, which
# is verified when an editable object is created.
HIDDEN_PREFIX = "__be_"
SNAPSHOT_PREFIX = HIDDEN_PREFIX + "s_"
KEY_PREFIX = HIDDEN_PREFIX + "k_"

# SQLite pseudo-column used as a row identity when the table has no primary key
ROWID = "rowid"


#############################################################################
# Row identity and edit metadata

class _EditContext(object):
    """Shared edit metadata: base table, identity and snapshot layout."""

    def __init__(self, table_object, key=None):
        self.table_object = table_object
        self.table = table_object.table
        self.store = table_object.store

        if key:
            key_names = [str(f) for f in key]
            for name in key_names:
                if name not in self.table.c:
                    raise ArgumentError("Unknown key column '%s'" % name)
            self.use_rowid = False
            self.key_names = key_names
        else:
            pk_columns = list(self.table.primary_key.columns)
            if pk_columns:
                self.use_rowid = False
                self.key_names = [c.name for c in pk_columns]
            elif _dialect_name(self.store) == "sqlite":
                # SQLite has an implicit rowid that identifies rows uniquely
                self.use_rowid = True
                self.key_names = [ROWID]
            else:
                raise NotEditableError(
                    "Table '%s' has no primary key and the backend has no "
                    "implicit row identity. Specify key= explicitly or add a "
                    "primary key." % getattr(self.table, "name", "?"))

        self.snapshot_columns = [c.name for c in self.table.columns]
        self.snapshot_labels = [SNAPSHOT_PREFIX + str(i)
                                for i in range(len(self.snapshot_columns))]
        self.key_labels = [KEY_PREFIX + str(i)
                           for i in range(len(self.key_names))]
        self.hidden_labels = self.snapshot_labels + self.key_labels

        for column in self.table.columns:
            if column.name.startswith(HIDDEN_PREFIX):
                raise DataObjectError(
                    "Column name '%s' is reserved for editable objects"
                    % column.name)

    def public_fields(self, fields=None):
        """Returns table fields without the identity columns."""
        fields = fields if fields is not None else self.table_object.fields
        key_set = set(self.key_names)
        return FieldList(*[f for f in fields if str(f) not in key_set])

    def root_statement(self, public_fields):
        """Builds the root statement: hidden snapshots, public columns and
        hidden identity columns."""
        table = self.table
        columns = []

        for column, label in zip(table.columns, self.snapshot_labels):
            columns.append(column.label(label))

        for field in public_fields:
            name = str(field)
            if name not in table.c:
                raise DataObjectError("Field '%s' is not a column of table "
                                      "'%s'" % (name, table.name))
            if name in self.hidden_labels:
                raise DataObjectError("Field name '%s' collides with a hidden "
                                      "column label" % name)
            columns.append(table.c[name].label(name))

        if self.use_rowid:
            columns.append(sqlalchemy.literal_column(ROWID)
                           .label(self.key_labels[0]))
        else:
            for name, label in zip(self.key_names, self.key_labels):
                columns.append(table.c[name].label(label))

        return select(columns, from_obj=table)

    def commit(self, records):
        return _commit(self, records)


def _dialect_name(store):
    try:
        return store.connectable.dialect.name
    except AttributeError:
        return None


def _source_column(expression, source):
    """Returns the name of a column of `source` if `expression` is a plain
    reference to it, otherwise ``None``. Computed expressions return
    ``None``."""
    element = expression.element if isinstance(expression, Label) else expression
    if isinstance(element, SAColumn) and element.table is source:
        return element.name
    return None


def _is_previous(source, previous):
    if source is previous:
        return True
    if isinstance(source, Alias):
        return getattr(source, "element", None) is previous
    return False


def _non_rewritable_reason(statement):
    """Returns a human readable reason why `statement` can't be written back,
    or ``None``."""
    if isinstance(statement, CompoundSelect):
        return "combined result (union, intersection or except)"
    if not isinstance(statement, Select):
        return "not a selectable result"
    group_by = getattr(statement, "_group_by_clause", None)
    if group_by is not None and len(list(group_by)) > 0:
        return "aggregation or distinct result"
    if getattr(statement, "_having", None) is not None:
        return "aggregation result"
    if getattr(statement, "_distinct", False):
        return "distinct result"
    return None


def _copy_limits(source, target):
    limit = getattr(source, "_limit_clause", None)
    offset = getattr(source, "_offset_clause", None)
    if limit is not None:
        target = target.limit(limit.value)
    if offset is not None:
        target = target.offset(offset.value)
    return target


def _rebuild(previous, ctx, expressions):
    """Builds a plain projection of `expressions` plus hidden columns from the
    `previous` statement."""
    columns = list(expressions)
    for label in ctx.hidden_labels:
        columns.append(previous.c[label].label(label))
    return select(columns, from_obj=previous)


def _align_field_map(ctx, statement, fields, output_map):
    """Aligns the output name based map produced by `_augment` with the public
    field names of the derived object.

    Most operations name their outputs exactly as the fields, but some (for
    example `append_constant_fields`) introduce values without a proper SQL
    name. In that case the public columns are aligned by position and the
    statement is re-labeled with the public field names, so that further
    operations and `columns()` can find them.

    Returns ``(statement, field_map)``.
    """

    if output_map is None:
        return statement, None

    field_names = [str(field) for field in fields]
    if all(name in output_map for name in field_names):
        return statement, dict((name, output_map[name]) for name in field_names)

    hidden = set(ctx.hidden_labels)
    output_names = [name for name in statement.c.keys() if name not in hidden]

    if len(output_names) != len(field_names):
        return statement, None

    columns = [statement.c[output].label(field_name)
                   for field_name, output in zip(field_names, output_names)]
    for label in ctx.hidden_labels:
        columns.append(statement.c[label].label(label))

    relabeled = select(columns, from_obj=statement)

    field_map = dict((field_name, output_map.get(output_name))
                        for field_name, output_name in zip(field_names,
                                                           output_names))
    return relabeled, field_map


def _augment(ctx, previous, previous_map, incoming):
    """Tries to carry hidden identity columns through `incoming`.

    `previous` is the receiver's statement and `previous_map` maps its public
    output names to physical column names of the base table (``None`` for
    computed columns).

    Returns ``(statement, field_map, reason)``. When the statement can not be
    written back, `reason` is a human readable explanation and `field_map` is
    ``None``. Otherwise `reason` is ``None``; individual computed fields might
    still map to ``None`` and be non-writable.
    """

    statement = incoming
    if isinstance(statement, Alias):
        element = getattr(statement, "element", None)
        if isinstance(element, Select) and not isinstance(element, CompoundSelect):
            statement = element

    reason = _non_rewritable_reason(statement)
    if reason:
        return statement, None, reason

    raw_columns = list(statement._raw_columns)
    froms = list(getattr(statement, "_from_obj", []) or [])

    # `previous.select(...)` (used by `sample`) produces an implicit
    # sub-query: the previous statement is its only "column", there is no
    # FROM and only ordering/limit is added.
    if (not froms and len(raw_columns) == 1
            and raw_columns[0] is previous):
        field_map = dict(previous_map)
        expressions = [previous.c[name] for name in previous_map]
        result = _rebuild(previous, ctx, expressions)
        result = _copy_limits(statement, result)
        return result, field_map, None

    if len(froms) != 1:
        return statement, None, "join or multiple sources"

    source = froms[0]
    if isinstance(source, Join):
        return statement, None, "join result"
    if not _is_previous(source, previous):
        return statement, None, "result derived from another source"

    hidden = set(ctx.hidden_labels)
    field_map = {}
    expressions = []

    for expression in raw_columns:
        output_name = getattr(expression, "name", None)
        if output_name is None:
            return statement, None, "unnamed computed column"
        if output_name in hidden:
            # Hidden column carried through by an operation: it is re-added
            # below with the canonical label.
            continue
        source_name = _source_column(expression, source)
        field_map[output_name] = previous_map.get(source_name) \
                                        if source_name is not None else None
        expressions.append(expression)

    result = _rebuild(source, ctx, expressions)
    result = result.where(statement._whereclause) \
                   if statement._whereclause is not None else result
    order_by = list(statement._order_by_clause) or None
    if order_by:
        result = result.order_by(*order_by)
    result = _copy_limits(statement, result)

    return result, field_map, None


#############################################################################
# Records

class EditableRecord(dict):
    """A dictionary-like record that knows its original physical row.

    The record exposes only the public fields. Values can be changed directly
    (``record["total"] = 120``); the change is written back to the matching
    physical column of the original row on commit.

    Records of results that can not be written back are read-only: any attempt
    to modify them raises :class:`NotEditableError`.
    """

    def __init__(self, data, owner=None, key=None, snapshot=None,
                 field_map=None, reason=None):
        super(EditableRecord, self).__init__(data)
        self._owner = owner
        self._key = key
        self._snapshot = snapshot
        self._original = dict(data)
        self._field_map = field_map or {}
        self._read_only_reason = reason

    @property
    def is_editable(self):
        return self._read_only_reason is None

    @property
    def key(self):
        """Opaque identity of the original row. Not part of the record."""
        return self._key

    def changes(self):
        """Returns a dictionary of changed public fields."""
        return dict((name, value) for name, value in self.items()
                    if name not in self._original
                    or value != self._original[name])

    def _check_writable(self, name):
        if self._read_only_reason is not None:
            raise NotEditableError("Record is read-only: %s"
                                   % self._read_only_reason)
        if name not in self._field_map:
            raise KeyError("Record has no field '%s'" % name)
        if self._field_map[name] is None:
            raise NotEditableError(
                "Field '%s' is derived and can not be written back" % name)

    def __setitem__(self, name, value):
        self._check_writable(name)
        super(EditableRecord, self).__setitem__(name, value)

    def update(self, *args, **kwargs):
        changes = dict(*args, **kwargs)
        for name in changes:
            self._check_writable(name)
        super(EditableRecord, self).update(changes)

    def setdefault(self, name, default=None):
        if name not in self:
            self._check_writable(name)
        return super(EditableRecord, self).setdefault(name, default)

    def __delitem__(self, name):
        raise NotEditableError("Fields can not be removed from a record")

    def pop(self, name, *default):
        raise NotEditableError("Fields can not be removed from a record")

    def popitem(self):
        raise NotEditableError("Fields can not be removed from a record")

    def clear(self):
        raise NotEditableError("Fields can not be removed from a record")

    def commit(self):
        """Commits this single record. See `EditableSQLStatement.commit`."""
        if self._owner is None:
            raise NotEditableError("Record is not attached to an editable "
                                   "object")
        return self._owner.commit([self])

    def clone(self):
        """Returns a detached copy of the record."""
        record = EditableRecord(dict(self), owner=self._owner, key=self._key,
                                snapshot=self._snapshot,
                                field_map=dict(self._field_map),
                                reason=self._read_only_reason)
        record._original = dict(self._original)
        return record

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, dict.__repr__(self))


#############################################################################
# Editable objects

class EditableSQLStatement(SQLDataObject):
    """A SQL statement that can be written back to its source table.

    Instances are created from a `SQLTable` or derived from another editable
    statement by operations. Operations that can not be written back produce
    read-only instances (:attr:`is_editable` is ``False``) with a reason in
    :attr:`not_editable_reason`.
    """

    _bubbles_info = {"abstract": True}

    def __init__(self, statement, store, fields=None, schema=None,
                 edit_ctx=None, field_map=None, not_editable_reason=None):
        super(EditableSQLStatement, self).__init__(store=store, schema=schema)

        self.statement = statement
        self.fields = fields if fields is not None else FieldList()
        self._edit_ctx = edit_ctx
        self._field_map = field_map if field_map is not None else {}
        self._not_editable_reason = not_editable_reason

        try:
            self.name = statement.name
        except AttributeError:
            self.name = "editable_statement_%s" % id(self)

    # -- Editability

    @property
    def is_editable(self):
        return self._edit_ctx is not None \
                and self._not_editable_reason is None

    @property
    def not_editable_reason(self):
        return self._not_editable_reason

    def as_editable(self):
        """Returns the receiver (already editable)."""
        return self

    def as_target(self):
        raise IsNotTargetError("Editable object is an edit source, it can not "
                               "be used as a target")

    # -- Representation

    def representations(self):
        return ["sql", "rows", "records"]

    def selectable(self):
        """Returns a selectable with the public columns only. Hidden identity
        columns are never exposed through composition or appending."""
        statement = self.statement
        columns = [statement.c[str(field)] for field in self.fields]
        return select(columns, from_obj=statement)

    def sql_statement(self):
        return self.statement

    def columns(self, fields=None):
        if fields is None:
            return self.statement.columns
        return [self.statement.c[str(field)] for field in fields]

    def column(self, field):
        return self.statement.c[str(field)]

    def __len__(self):
        counter = sqlalchemy.sql.func.count(1)
        statement = select([counter], from_obj=self.statement)
        return self.store.connectable.scalar(statement)

    # -- Derivation

    def clone_statement(self, statement=None, fields=None):
        """Creates a derived editable (or read-only) statement object."""
        if statement is None:
            statement = self.statement
        fields = fields if fields is not None else self.fields.clone()

        if self._edit_ctx is None or self._not_editable_reason is not None:
            reason = self._not_editable_reason or "source is not editable"
            return EditableSQLStatement(statement, self.store, fields=fields,
                                        schema=self.schema,
                                        not_editable_reason=reason)

        augmented, field_map, reason = _augment(self._edit_ctx, self.statement,
                                                self._field_map, statement)
        if field_map is not None:
            augmented, field_map = _align_field_map(self._edit_ctx, augmented,
                                                    fields, field_map)
        return EditableSQLStatement(augmented, self.store, fields=fields,
                                    schema=self.schema,
                                    edit_ctx=self._edit_ctx,
                                    field_map=field_map,
                                    not_editable_reason=reason)

    # -- Reading

    def _projection_indexes(self):
        keys = list(self.statement.c.keys())
        indexes = []
        for position, field in enumerate(self.fields):
            name = str(field)
            if name in keys:
                indexes.append(keys.index(name))
            elif position < len(keys):
                indexes.append(position)
            else:
                raise DataObjectError("Unable to locate field '%s' in the "
                                      "statement" % name)
        return indexes

    def rows(self):
        indexes = self._projection_indexes()
        for row in self.store.execute(self.statement):
            yield tuple(row[i] for i in indexes)

    def records(self):
        return self._records_iterator()

    def _records_iterator(self):
        indexes = self._projection_indexes()
        public_names = self.fields.names()

        if not self.is_editable:
            reason = self._not_editable_reason or "not editable"
            for row in self.store.execute(self.statement):
                data = dict((name, row[index])
                                for name, index in zip(public_names, indexes))
                yield EditableRecord(data, owner=self, reason=reason)
            return

        keys = list(self.statement.c.keys())
        ctx = self._edit_ctx
        snapshot_indexes = [keys.index(label) for label in ctx.snapshot_labels]
        key_indexes = [keys.index(label) for label in ctx.key_labels]

        for row in self.store.execute(self.statement):
            data = dict((name, row[index])
                            for name, index in zip(public_names, indexes))
            snapshot = tuple(row[i] for i in snapshot_indexes)
            key = tuple(row[i] for i in key_indexes)
            yield EditableRecord(data, owner=self, key=key, snapshot=snapshot,
                                 field_map=dict(self._field_map))

    # -- Writing

    def commit(self, records=None):
        """Writes changed records back to the source table.

        `records` is an iterable of `EditableRecord` objects. Records without
        changes are ignored. Only the changed physical columns are updated;
        the identity column is never modified and unchanged fields keep their
        value. The whole batch is applied in one transaction: if any record
        fails (conflict, database error), nothing is written and an exception
        is raised.

        Returns the number of updated rows.
        """

        if not self.is_editable:
            raise NotEditableError("Object is not editable: %s"
                                   % (self._not_editable_reason or
                                      "no source identity"))
        return self._edit_ctx.commit(records)


class EditableSQLTable(EditableSQLStatement):
    """An opt-in editable view of a `SQLTable`.

    Create it with :meth:`SQLTable.as_editable` or the ``as_editable``
    operation. The public field list excludes the identity (primary key)
    column; reading ``rows()`` or ``records()`` exposes only the public
    layout.
    """

    _bubbles_info = {"abstract": True}

    def __init__(self, table_object, key=None):
        self.table_object = table_object
        self.table = table_object.table

        ctx = _EditContext(table_object, key=key)
        public_fields = ctx.public_fields()
        root = ctx.root_statement(public_fields)
        field_map = dict((str(f), str(f)) for f in public_fields)

        super(EditableSQLTable, self).__init__(
            root, table_object.store, fields=public_fields,
            schema=table_object.schema, edit_ctx=ctx, field_map=field_map)

        self.name = getattr(table_object, "name", self.table.name)


#############################################################################
# Commit implementation

def _key_condition(ctx, key):
    if ctx.use_rowid:
        return [sqlalchemy.literal_column(ROWID) == key[0]]

    conditions = []
    for name, value in zip(ctx.key_names, key):
        column = ctx.table.c[name]
        conditions.append(column.is_(None) if value is None
                          else column == value)
    return conditions


def _snapshot_condition(ctx, snapshot):
    conditions = []
    for name, value in zip(ctx.snapshot_columns, snapshot):
        column = ctx.table.c[name]
        conditions.append(column.is_(None) if value is None
                          else column == value)
    return conditions


def _changes_to_columns(ctx, record):
    """Validates the record changes and returns a dict of physical column ->
    new value."""

    changes = record.changes()
    if not changes:
        return {}

    read_only = getattr(record, "_read_only_reason", None)
    if read_only is not None:
        raise NotEditableError("Record is read-only: %s" % read_only)

    owner = getattr(record, "_owner", None)
    owner_ctx = getattr(owner, "_edit_ctx", None)
    if owner_ctx is not ctx:
        raise NotEditableError("Record was read from a different editable "
                               "object and can not be committed here")

    if record._key is None or record._snapshot is None:
        raise NotEditableError("Record does not carry row identity and can "
                               "not be written back")

    key_set = set(ctx.key_names)
    columns = {}
    for name, value in changes.items():
        physical = record._field_map.get(name)
        if physical is None:
            raise NotEditableError("Field '%s' is derived and can not be "
                                   "written back" % name)
        if physical in key_set:
            raise NotEditableError("Identity column '%s' can not be modified"
                                   % physical)
        columns[physical] = value
    return columns


def _transaction(connectable):
    """Returns ``(connection, transaction, close)`` for the connectable."""
    if isinstance(connectable, Engine):
        connection = connectable.connect()
        return connection, connection.begin(), True

    connection = connectable
    if hasattr(connection, "in_transaction") and connection.in_transaction():
        return connection, connection.begin_nested(), False

    return connection, connection.begin(), False


def _commit(ctx, records):
    if records is None:
        raise ArgumentError("Records to commit should be provided")

    pending = []
    for record in records:
        if not isinstance(record, EditableRecord):
            raise ArgumentError("Only EditableRecord objects can be committed,"
                                " got %s" % type(record).__name__)
        columns = _changes_to_columns(ctx, record)
        if columns:
            pending.append((record, columns))

    if not pending:
        return 0

    table = ctx.table
    connection, transaction, close = _transaction(ctx.store.connectable)

    try:
        for record, columns in pending:
            conditions = _key_condition(ctx, record._key)
            conditions += _snapshot_condition(ctx, record._snapshot)

            statement = table.update().where(sqlalchemy.and_(*conditions)) \
                                      .values(columns)
            result = connection.execute(statement)

            if result.rowcount != 1:
                raise EditConflictError(
                    "Row has been modified or deleted since it was read "
                    "(key: %s)" % (record._key,),
                    key=record._key, changes=dict(record.changes()))

        transaction.commit()
    except Exception:
        transaction.rollback()
        raise
    finally:
        if close:
            connection.close()

    for record, columns in pending:
        values = dict(zip(ctx.snapshot_columns, record._snapshot))
        values.update(columns)
        record._snapshot = tuple(values[name]
                                     for name in ctx.snapshot_columns)
        record._original = dict(record)

    return len(pending)
