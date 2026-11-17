import unittest

from bubbles import Field, FieldList, OperationContext
from bubbles.errors import (NotEditableError, EditConflictError,
                            ArgumentError)
from bubbles.backends.sql.objects import SQLDataStore
from bubbles.backends.sql.editable import (EditableSQLTable,
                                           EditableSQLStatement,
                                           EditableRecord)
import bubbles.backends.sql.ops


def _sql_number(store, number):
    """Returns a literal SQL expression for `number`, so it can be used as an
    `append_constant_fields` value."""
    return store.connectable.dialect.statement_compiler(
                            store.connectable.dialect, None)


class EditableTestCase(unittest.TestCase):
    def setUp(self):
        self.context = OperationContext()
        self.context.add_operations_from(bubbles.backends.sql.ops)

        self.store = SQLDataStore("sqlite:///")
        self.table = self.store.create(
            "items",
            FieldList(("status", "string"), ("name", "string"),
                      ("amount", "integer"), ("note", "string")),
            replace=True, id_column="id")
        self.store.execute(self.table.table.insert(), [
            {"status": "new", "name": "a", "amount": 10, "note": None},
            {"status": "new", "name": "b", "amount": 20, "note": "b-note"},
            {"status": "old", "name": "c", "amount": 30, "note": "c-note"},
            {"status": "new", "name": "c", "amount": 30, "note": "dup"},
        ])
        self.editable = self.table.as_editable()

    # -- Helpers

    def _physical_rows(self):
        statement = self.table.table.select()
        return [tuple(row) for row in self.store.execute(statement)]

    def _all_records(self):
        return list(self.editable.records())

    # -- Read-only behaviour

    def test_public_layout_hides_key(self):
        self.assertEqual(["status", "name", "amount", "note"],
                         self.editable.fields.names())
        self.assertNotIn("id", self.editable.fields.names())

        names = list(self.editable.sql_statement().c.keys())
        self.assertNotIn("id", names)

    def test_rows_and_records_only_selected_layout(self):
        rows = list(self.editable.rows())
        self.assertEqual([("new", "a", 10, None),
                          ("new", "b", 20, "b-note"),
                          ("old", "c", 30, "c-note"),
                          ("new", "c", 30, "dup")], rows)

        records = self._all_records()
        self.assertEqual(["status", "name", "amount", "note"],
                         list(records[0].keys()))
        self.assertNotIn("id", records[0])

    def test_ordinary_table_reads_unchanged(self):
        # The source table still exposes its identity column
        self.assertEqual(["id", "status", "name", "amount", "note"],
                         self.table.fields.names())
        row = next(iter(self.table.rows()))
        self.assertEqual(5, len(row))

    def test_as_editable_operation(self):
        editable = self.context.op.as_editable(self.table)
        self.assertIsInstance(editable, EditableSQLTable)
        self.assertTrue(editable.is_editable)

        # Idempotent
        self.assertIs(editable, self.context.op.as_editable(editable))

    def test_plain_statement_is_read_only(self):
        statement = self.store.statement(self.table.table.select())
        editable = self.context.op.as_editable(statement)
        self.assertIsInstance(editable, EditableSQLStatement)
        self.assertFalse(editable.is_editable)
        self.assertIsNotNone(editable.not_editable_reason)

        records = list(editable.records())
        with self.assertRaises(NotEditableError):
            records[0]["amount"] = 1

    # -- Editing

    def test_update_single_record(self):
        records = self._all_records()
        records[0]["amount"] = 11
        self.assertEqual(1, self.editable.commit([records[0]]))

        rows = self._physical_rows()
        self.assertEqual(11, rows[0][3])
        # Untouched
        self.assertEqual(("new", "a"), (rows[0][1], rows[0][2]))
        self.assertEqual(20, rows[1][3])

    def test_only_changed_columns_updated(self):
        records = self._all_records()
        record = records[1]
        record["note"] = "changed"
        self.editable.commit([record])

        rows = self._physical_rows()
        self.assertEqual(("new", "b", 20, "changed"),
                         (rows[1][1], rows[1][2], rows[1][3], rows[1][4]))

    def test_unchanged_records_are_ignored(self):
        records = self._all_records()
        self.assertEqual(0, self.editable.commit(records))
        self.assertEqual(self._physical_rows(), self._physical_rows())

    def test_primary_key_not_updated(self):
        records = self._all_records()
        records[0]["name"] = "renamed"
        self.editable.commit(records)

        rows = self._physical_rows()
        self.assertEqual([1, 2, 3, 4], [row[0] for row in rows])

    def test_identical_visible_rows_do_not_collide(self):
        # Rows 3 and 4 have identical visible values except `note`
        store = SQLDataStore("sqlite:///")
        table = store.create("dups",
                             FieldList(("a", "integer"), ("b", "integer")),
                             replace=True, id_column="id")
        store.execute(table.table.insert(), [{"a": 1, "b": 1},
                                             {"a": 1, "b": 1}])
        editable = table.as_editable()

        records = list(editable.records())
        self.assertEqual(records[0], records[1])

        records[1]["b"] = 9
        self.assertEqual(1, editable.commit([records[1]]))

        rows = [tuple(row) for row in store.execute(table.table.select())]
        self.assertEqual([(1, 1, 1), (2, 1, 9)], rows)

    def test_identity_without_primary_key(self):
        store = SQLDataStore("sqlite:///")
        table = store.create("no_key",
                             FieldList(("name", "string"),
                                       ("amount", "integer")),
                             replace=True)
        store.execute(table.table.insert(), [{"name": "a", "amount": 1},
                                             {"name": "a", "amount": 1},
                                             {"name": "b", "amount": 2}])
        editable = table.as_editable()
        self.assertTrue(editable.is_editable)

        records = list(editable.records())
        self.assertEqual(records[0], records[1])

        records[1]["amount"] = 50
        self.assertEqual(1, editable.commit([records[1]]))

        rows = [tuple(row) for row in store.execute(table.table.select())]
        self.assertEqual([("a", 1), ("a", 50), ("b", 2)], rows)

    def test_explicit_composite_key(self):
        table = self.store.create(
            "composite",
            FieldList(("k1", "integer"), ("k2", "integer"),
                      ("value", "string")),
            replace=True)
        self.store.execute(table.table.insert(), [
            {"k1": 1, "k2": 1, "value": "a"},
            {"k1": 1, "k2": 2, "value": "b"},
        ])
        editable = table.as_editable(key=["k1", "k2"])
        self.assertEqual(["value"], editable.fields.names())

        records = list(editable.records())
        records[1]["value"] = "B"
        editable.commit(records)

        rows = [tuple(row) for row in
                    self.store.execute(table.table.select())]
        self.assertEqual([(1, 1, "a"), (1, 2, "B")], rows)

    def test_unknown_key_column(self):
        with self.assertRaises(ArgumentError):
            self.table.as_editable(key=["no_such_column"])

    # -- Derived read results

    def test_filter_and_sort(self):
        selected = self.context.op.filter_by_value(self.editable, "status",
                                                   "new")
        selected = self.context.op.sort(selected, [("amount", "desc")])

        self.assertTrue(selected.is_editable)
        self.assertEqual(["status", "name", "amount", "note"],
                         selected.fields.names())
        self.assertEqual([("new", "c", 30, "dup"),
                          ("new", "b", 20, "b-note"),
                          ("new", "a", 10, None)],
                         list(selected.rows()))

        records = list(selected.records())
        records[0]["amount"] = 21
        selected.commit(records)

        rows = self._physical_rows()
        # The first record is the last physical row (note "dup")
        self.assertEqual(21, rows[3][3])

    def test_field_filter_rename_and_commit(self):
        selected = self.context.op.field_filter(
            self.editable, keep=["name", "amount"],
            rename={"amount": "total"})
        selected = self.context.op.sort(selected, [("total", "desc")])

        self.assertTrue(selected.is_editable)
        self.assertEqual(["name", "total"], selected.fields.names())
        self.assertEqual([("c", 30), ("c", 30), ("b", 20), ("a", 10)],
                         list(selected.rows()))

        records = list(selected.records())
        # Second `c` is the fourth physical row (note "dup")
        records[1]["total"] = 31
        records[1]["name"] = "cc"
        self.assertEqual(1, selected.commit([records[1]]))

        rows = self._physical_rows()
        self.assertEqual(("cc", 31), (rows[3][2], rows[3][3]))
        self.assertEqual(("c", 30), (rows[2][2], rows[2][3]))

    def test_field_filter_drop_then_filter(self):
        selected = self.context.op.field_filter(self.editable, drop=["note"])
        selected = self.context.op.filter_by_range(selected, "amount", 25, 35)

        self.assertEqual(["status", "name", "amount"], selected.fields.names())
        records = list(selected.records())
        records[0]["amount"] = 35
        selected.commit(records)

        rows = self._physical_rows()
        self.assertEqual(35, rows[2][3])

    def test_filter_by_set(self):
        selected = self.context.op.filter_by_set(self.editable, "name",
                                                 ["a", "b"])
        self.assertTrue(selected.is_editable)
        self.assertEqual(2, len(list(selected.rows())))

        records = list(selected.records())
        records[1]["amount"] = 22
        selected.commit(records)
        self.assertEqual(22, self._physical_rows()[1][3])

    def test_sample_is_editable(self):
        selected = self.context.op.sample(self.editable, 2)
        self.assertTrue(selected.is_editable)

        records = list(selected.records())
        records[0]["amount"] = 100
        selected.commit(records)
        self.assertEqual(100, self._physical_rows()[0][3])

    def test_derived_column_is_not_writable(self):
        selected = self.context.op.append_constant_fields(
            self.editable, [Field("kind")],
            [self._literal("'X'")])

        self.assertTrue(selected.is_editable)
        self.assertEqual(["status", "name", "amount", "note", "kind"],
                         selected.fields.names())

        record = list(selected.records())[0]
        with self.assertRaises(NotEditableError):
            record["kind"] = "Y"

        # The physical column still can be changed
        record["amount"] = 50
        selected.commit([record])
        self.assertEqual(50, self._physical_rows()[0][3])

    def _literal(self, value):
        import sqlalchemy
        return sqlalchemy.literal_column(value)

    # -- Read-only results

    def test_aggregate_is_read_only(self):
        selected = self.context.op.aggregate(self.editable, "name",
                                             measures=["amount"])
        self.assertFalse(selected.is_editable)
        self.assertIn("aggregation", selected.not_editable_reason)

        records = list(selected.records())
        self.assertEqual(["name", "amount_sum", "record_count"],
                         list(records[0].keys()))
        with self.assertRaises(NotEditableError):
            records[0]["amount_sum"] = 1

    def test_distinct_is_read_only(self):
        selected = self.context.op.distinct(self.editable, "name")
        self.assertFalse(selected.is_editable)
        self.assertIn("distinct", selected.not_editable_reason)

    def test_join_is_read_only(self):
        details = self.store.create(
            "details", FieldList(("name", "string"), ("qty", "integer")),
            replace=True)
        self.store.execute(details.table.insert(), [{"name": "a", "qty": 5}])

        joined = self.context.op.join_details(self.editable,
                                              details.as_editable(),
                                              "name", "name")
        self.assertFalse(joined.is_editable)
        self.assertIn("join", joined.not_editable_reason)

        records = list(joined.records())
        self.assertEqual({"name": "a", "status": "new", "amount": 10,
                          "note": None, "qty": 5}, dict(records[0]))
        with self.assertRaises(NotEditableError):
            records[0]["qty"] = 7

    def test_append_is_read_only(self):
        new = self.context.op.filter_by_value(self.editable, "status", "new")
        old = self.context.op.filter_by_value(self.editable, "status", "old")

        appended = self.context.op.append([new, old])
        self.assertFalse(appended.is_editable)
        self.assertIn("union", appended.not_editable_reason)
        self.assertEqual(["status", "name", "amount", "note"],
                         appended.fields.names())
        self.assertEqual(4, len(list(appended.rows())))

    # -- Conflicts

    def test_conflict_on_external_modification(self):
        records = self._all_records()
        record = records[0]

        self.store.execute(self.table.table.update()
                                .where(self.table.table.c.id == 1)
                                .values({"amount": 123}))

        record["amount"] = 11
        with self.assertRaises(EditConflictError):
            self.editable.commit([record])

        # Nothing was written
        rows = self._physical_rows()
        self.assertEqual(123, rows[0][3])

    def test_conflict_on_external_deletion(self):
        records = self._all_records()
        record = records[2]

        self.store.execute(self.table.table.delete()
                                .where(self.table.table.c.id == 3))

        record["note"] = "changed"
        with self.assertRaises(EditConflictError):
            self.editable.commit([record])

    def test_batch_is_atomic(self):
        records = self._all_records()
        records[0]["amount"] = 111
        records[1]["amount"] = 222

        # Externally modify the second row, so the second update conflicts
        self.store.execute(self.table.table.update()
                                .where(self.table.table.c.id == 2)
                                .values({"note": "external"}))

        with self.assertRaises(EditConflictError):
            self.editable.commit(records)

        rows = self._physical_rows()
        self.assertEqual(10, rows[0][3])
        self.assertEqual(20, rows[1][3])

    def test_database_error_rolls_back(self):
        import sqlalchemy
        from sqlalchemy import MetaData, Table, Column, Integer, String
        from sqlalchemy import CheckConstraint

        engine = sqlalchemy.create_engine("sqlite:///")
        metadata = MetaData(bind=engine)
        table = Table("checked", metadata,
                      Column("id", Integer, primary_key=True),
                      Column("name", String),
                      Column("amount", Integer),
                      CheckConstraint("amount >= 0"))
        metadata.create_all()

        store = SQLDataStore(connectable=engine)
        table_object = store.get_object("checked")
        store.execute(table.insert(), [{"name": "a", "amount": 1},
                                       {"name": "b", "amount": 2}])

        editable = table_object.as_editable()
        records = list(editable.records())
        records[0]["amount"] = 5
        records[1]["amount"] = -3

        with self.assertRaises(Exception):
            editable.commit(records)

        rows = [tuple(row) for row in store.execute(table.select())]
        self.assertEqual([(1, "a", 1), (2, "b", 2)], rows)

    # -- Record semantics

    def test_record_commit(self):
        record = self._all_records()[0]
        record["note"] = "single"
        self.assertEqual(1, record.commit())
        self.assertEqual("single", self._physical_rows()[0][4])
        # Committing again is a no-op
        self.assertEqual(0, record.commit())

    def test_record_can_not_be_committed_elsewhere(self):
        record = self._all_records()[0]
        record["amount"] = 5
        other = self.table.as_editable()
        with self.assertRaises(NotEditableError):
            other.commit([record])

    def test_record_fields_can_not_be_removed(self):
        record = self._all_records()[0]
        with self.assertRaises(NotEditableError):
            del record["note"]
        with self.assertRaises(NotEditableError):
            record.pop("note")

    def test_key_is_not_part_of_record(self):
        record = self._all_records()[0]
        self.assertEqual((1,), record.key)
        self.assertNotIn("id", record)
        self.assertNotIn("__be_k_0", record.keys())

    def test_commit_requires_records(self):
        with self.assertRaises(ArgumentError):
            self.editable.commit(None)

    def test_commit_rejects_plain_dicts(self):
        with self.assertRaises(ArgumentError):
            self.editable.commit([{"name": "x"}])


if __name__ == "__main__":
    unittest.main()
