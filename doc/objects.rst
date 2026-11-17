#######
Objects
#######

Representations
===============

* `sql` – SQLAlchemy Selectable statement
* `sql_table` – SQLAlchemy Table object
* `rows` – python iterator of anonymous tuples
* `records` – python iterator of named records

Planned representations:

* `mongo`
* `csv` – raw CSV stream (for targets that might process it directly)


Editable objects
================

Ordinary SQL objects are read-only: they provide rows, records and statements,
but there is no way to write a modified record back to the row it came from.
An *editable* object is an opt-in view of a `SQLTable` that adds record
editing. Ordinary reads of the table are not affected.

.. code-block:: python

    table = store.get_object("items")
    editable = table.as_editable()       # or ctx.op.as_editable(table)

    selected = ctx.op.filter_by_value(editable, "status", "new")
    selected = ctx.op.field_filter(selected, keep=["name", "amount"],
                                   rename={"amount": "total"})
    selected = ctx.op.sort(selected, [("total", "desc")])

    records = list(selected.records())
    records[0]["total"] = 120
    selected.commit(records)

Properties:

* **The public layout only.** The identity column (usually the primary key) is
  hidden: it is not part of ``fields``, ``rows()`` or ``records()``. The same
  applies to renamed and dropped fields – they are presented exactly like in
  the selected layout, with the original column types.
* **Derivation keeps the identity.** Native row filters, sorting and
  ``field_filter`` (including renames) are ordinary SQL operations on top of
  the editable statement. The hidden identity and snapshot columns are carried
  through them, so a record always knows the original physical row.
* **Only changed columns are written.** On commit, only the physical columns
  of the changed fields are updated. Unchanged fields and the identity column
  are not touched.
* **No wrong-row writes.** The hidden identity distinguishes rows even when
  two records have completely identical visible values.
* **Conflicts are detected.** The update is guarded by an optimistic
  ``WHERE key = ? AND <all snapshot columns match>`` predicate. If the original
  row was modified or deleted externally, no row matches and
  :class:`bubbles.errors.EditConflictError` is raised.
* **The batch is atomic.** All submitted records are written in one
  transaction. If any record fails (conflict or database error), nothing is
  written.
* **Results that can not be written back are read-only.** Aggregation, join,
  distinct, union/append and similar results have ``is_editable`` set to
  ``False`` and a reason in ``not_editable_reason``. They can still be read,
  but their records reject modification with
  :class:`bubbles.errors.NotEditableError`.

The identity is the primary key. For tables without a primary key on SQLite the
implicit ``rowid`` is used. A different (composite) key can be specified
explicitly::

    editable = table.as_editable(key=["country", "code"])



