#
# Copyright 2024 Capital One Services, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Compare two Pandas DataFrames

Originally this package was meant to provide similar functionality to
PROC COMPARE in SAS - i.e. human-readable reporting on the difference between
two dataframes.
"""
import logging
import os
from copy import deepcopy
from typing import Any, Dict, List, Optional, Union, cast

import pandas as pd
import snowflake.snowpark as sp
from ordered_set import OrderedSet
from snowflake.snowpark import Window
from snowflake.snowpark.functions import (
    abs,
    array_contains,
    col,
    is_null,
    lit,
    monotonically_increasing_id,
    row_number,
    trim,
    upper,
    when,
)

from datacompy.base import BaseCompare

LOG = logging.getLogger(__name__)


# Used for checking equality with decimal(X, Y) types. Otherwise treated as the string "decimal".
def decimal_comparator():
    class DecimalComparator(str):
        def __eq__(self, other):
            return len(other) >= 7 and other[0:7] == "decimal"

    return DecimalComparator("decimal")


NUMERIC_SPARK_TYPES = [
    "tinyint",
    "smallint",
    "int",
    "bigint",
    "float",
    "double",
    decimal_comparator(),
]


class TableCompare(BaseCompare):
    """Comparison class to be used to compare whether two dataframes as equal.

    Both df1 and df2 should be dataframes containing all of the join_columns,
    with unique column names. Differences between values are compared to
    abs_tol + rel_tol * abs(df2['value']).

    Parameters
    ----------
    session: snowflake.snowpark.session
        Session with the required connection session info for user and targeted tables
    df1 : pandas ``DataFrame``
        First dataframe to check
    df2 : pandas ``DataFrame``
        Second dataframe to check
    join_columns : list or str, optional
        Column(s) to join dataframes on.  If a string is passed in, that one
        column will be used.
    abs_tol : float, optional
        Absolute tolerance between two values.
    rel_tol : float, optional
        Relative tolerance between two values.
    df1_name : str, optional
        A string name for the first dataframe.  This allows the reporting to
        print out an actual name instead of "df1", and allows human users to
        more easily track the dataframes.
    df2_name : str, optional
        A string name for the second dataframe
    ignore_spaces : bool, optional
        Flag to strip whitespace (including newlines) from string columns (including any join
        columns)

    Attributes
    ----------
    df1_unq_rows : pandas ``DataFrame``
        All records that are only in df1 (based on a join on join_columns)
    df2_unq_rows : pandas ``DataFrame``
        All records that are only in df2 (based on a join on join_columns)
    """

    def __init__(
        self,
        session: sp.Session,
        df1: str,
        df2: str,
        join_columns: Optional[Union[List[str], str]],
        abs_tol: float = 0,
        rel_tol: float = 0,
        ignore_spaces: bool = False,
    ) -> None:
        if join_columns is None:
            raise Exception("join_columns cannot be None")
        elif not join_columns:
            raise Exception("join_columns is empty")
        elif isinstance(join_columns, (str, int, float)):
            self.join_columns = [str(join_columns)]
        else:
            self.join_columns = [str(col) for col in cast(List[str], join_columns)]

        self._any_dupes: bool = False
        self.session = session
        self.df1 = df1
        self.df2 = df2
        self.df1_name = self.df1.table_name.replace(".", "_").upper()
        self.df2_name = self.df2.table_name.replace(".", "_").upper()
        self.abs_tol = abs_tol
        self.rel_tol = rel_tol
        self.ignore_spaces = ignore_spaces
        self.df1_unq_rows: sp.DataFrame
        self.df2_unq_rows: sp.DataFrame
        self.intersect_rows: sp.DataFrame
        self.column_stats: List[Dict[str, Any]] = []
        self._compare(ignore_spaces=ignore_spaces)

    @property
    def df1(self) -> sp.Table:
        return self._df1

    @df1.setter
    def df1(self, df1: str) -> None:
        """Check that it is a dataframe and has the join columns"""
        self._df1 = self.session.table(df1)
        self._validate_dataframe(self.df1)

    @property
    def df2(self) -> sp.Table:
        return self._df2

    @df2.setter
    def df2(self, df2: str) -> None:
        """Check that it is a dataframe and has the join columns"""
        self._df2 = self.session.table(df2)
        self._validate_dataframe(self.df2)

    def _validate_dataframe(self, df: sp.Table) -> None:
        """Check that it is a dataframe and has the join columns

        Parameters
        ----------
        df : snowflake.session.Table
            Snowflake Snowpark table object
        """
        if not isinstance(df, sp.Table):
            raise TypeError(f"{df.table_name} must be a valid table")

        # Check if join_columns are present in the dataframe
        if not set(self.join_columns).issubset(set(df.columns)):
            raise ValueError(f"{df.table_name} must have all columns from join_columns")

        if df.drop_duplicates(self.join_columns).count() < df.count():
            self._any_dupes = True

    def _compare(self, ignore_spaces: bool) -> None:
        """Actually run the comparison.  This tries to run df1.equals(df2)
        first so that if they're truly equal we can tell.

        This method will log out information about what is different between
        the two dataframes, and will also return a boolean.
        """
        LOG.info(f"Number of columns in common: {len(self.intersect_columns())}")
        LOG.debug("Checking column overlap")
        for col in self.df1_unq_columns():
            LOG.info(f"Column in df1 and not in df2: {col}")
        LOG.info(
            f"Number of columns in df1 and not in df2: {len(self.df1_unq_columns())}"
        )
        for col in self.df2_unq_columns():
            LOG.info(f"Column in df2 and not in df1: {col}")
        LOG.info(
            f"Number of columns in df2 and not in df1: {len(self.df2_unq_columns())}"
        )
        LOG.debug("Merging dataframes")
        self._dataframe_merge(ignore_spaces)
        self._intersect_compare(ignore_spaces)
        if self.matches():
            LOG.info("df1 matches df2")
        else:
            LOG.info("df1 does not match df2")

    def df1_unq_columns(self) -> OrderedSet[str]:
        """Get columns that are unique to df1"""
        return cast(
            OrderedSet[str], OrderedSet(self.df1.columns) - OrderedSet(self.df2.columns)
        )

    def df2_unq_columns(self) -> OrderedSet[str]:
        """Get columns that are unique to df2"""
        return cast(
            OrderedSet[str], OrderedSet(self.df2.columns) - OrderedSet(self.df1.columns)
        )

    def intersect_columns(self) -> OrderedSet[str]:
        """Get columns that are shared between the two dataframes"""
        return OrderedSet(self.df1.columns) & OrderedSet(self.df2.columns)

    def _dataframe_merge(self, ignore_spaces: bool) -> None:
        """Merge df1 to df2 on the join columns, to get df1 - df2, df2 - df1
        and df1 & df2

        Joins on the ``join_columns``.
        """
        LOG.debug("Outer joining")

        df1 = self.df1
        df2 = self.df2
        temp_join_columns = deepcopy(self.join_columns)

        if self._any_dupes:
            LOG.debug("Duplicate rows found, deduping by order of remaining fields")
            # setting internal index
            LOG.info("Adding internal index to dataframes")
            df1 = df1.withColumn("__index", monotonically_increasing_id())
            df2 = df2.withColumn("__index", monotonically_increasing_id())

            # Create order column for uniqueness of match
            order_column = temp_column_name(df1, df2)
            df1 = df1.join(
                _generate_id_within_group(df1, temp_join_columns, order_column),
                on="__index",
                how="inner",
            ).drop("__index")
            df2 = df2.join(
                _generate_id_within_group(df2, temp_join_columns, order_column),
                on="__index",
                how="inner",
            ).drop("__index")
            temp_join_columns.append(order_column)

            # drop index
            LOG.info("Dropping internal index")
            df1 = df1.drop("__index")
            df2 = df2.drop("__index")

        outer_join = df1.with_column("merge", lit(True)).join(
            df2.with_column("merge", lit(True)),
            on=self.join_columns,
            how="outer",
            lsuffix=f"_{self.df1_name}",
            rsuffix=f"_{self.df2_name}",
        )

        # Create join indicator
        outer_join = outer_join.with_column(
            "_merge",
            when(
                outer_join[f"MERGE_{self.df1_name}"]
                & outer_join[f"MERGE_{self.df2_name}"],
                lit("BOTH"),
            )
            .when(
                outer_join[f"MERGE_{self.df1_name}"]
                & outer_join[f"MERGE_{self.df2_name}"].is_null(),
                lit("LEFT_ONLY"),
            )
            .when(
                outer_join[f"MERGE_{self.df1_name}"].is_null()
                & outer_join[f"MERGE_{self.df2_name}"],
                lit("RIGHT_ONLY"),
            ),
        )

        # Clean up temp columns for duplicate row matching
        if self._any_dupes:
            outer_join = outer_join.select_expr(
                f"* EXCLUDE ({order_column}_{self.df1_name}, {order_column}_{self.df2_name})"
            )
            df1 = df1.drop(order_column)
            df2 = df2.drop(order_column)

        # Capitalization required - clean up
        df1_cols = get_merged_columns(df1, outer_join, self.df1_name)
        df2_cols = get_merged_columns(df2, outer_join, self.df2_name)

        LOG.debug("Selecting df1 unique rows")
        self.df1_unq_rows = outer_join[outer_join["_merge"] == "LEFT_ONLY"][df1_cols]
        self.df1_unq_rows.rename(dict(zip(self.df1_unq_rows.columns, df1.columns)))

        LOG.debug("Selecting df2 unique rows")
        self.df2_unq_rows = outer_join[outer_join["_merge"] == "RIGHT_ONLY"][df2_cols]
        self.df2_unq_rows.rename(dict(zip(self.df2_unq_rows.columns, df2.columns)))
        LOG.info(f"Number of rows in df1 and not in df2: {self.df1_unq_rows.count()}")
        LOG.info(f"Number of rows in df2 and not in df1: {self.df2_unq_rows.count()}")

        LOG.debug("Selecting intersecting rows")
        self.intersect_rows = outer_join[outer_join["_merge"] == "BOTH"]
        LOG.info(
            f"Number of rows in df1 and df2 (not necessarily equal): {self.intersect_rows.count()}"
        )

    def _intersect_compare(self, ignore_spaces: bool) -> None:
        """Run the comparison on the intersect dataframe

        This loops through all columns that are shared between df1 and df2, and
        creates a column column_match which is True for matches, False
        otherwise.
        """
        LOG.debug("Comparing intersection")
        max_diff: float
        null_diff: int
        row_cnt = self.intersect_rows.count()
        for column in self.intersect_columns():
            col1_dtype, _ = _get_column_dtypes(self.df1, column, column)
            col2_dtype, _ = _get_column_dtypes(self.df2, column, column)

            if column in self.join_columns:
                match_cnt = row_cnt
                col_match = ""
                max_diff = 0
                null_diff = 0
            else:
                col_1 = column + "_" + self.df1_name
                col_2 = column + "_" + self.df2_name
                col_match = column + "_match"
                self.intersect_rows = columns_equal(
                    self.intersect_rows,
                    col_1,
                    col_2,
                    col_match,
                    self.rel_tol,
                    self.abs_tol,
                    ignore_spaces,
                )
                match_cnt = (
                    self.intersect_rows.select(col_match)
                    .where(col(col_match) == True)  # noqa: E712
                    .count()
                )
                max_diff = calculate_max_diff(
                    self.intersect_rows, col_1, col_2, col1_dtype, col2_dtype
                )
                null_diff = calculate_null_diff(self.intersect_rows, col_1, col_2)

            if row_cnt > 0:
                match_rate = float(match_cnt) / row_cnt
            else:
                match_rate = 0
            LOG.info(f"{column}: {match_cnt} / {row_cnt} ({match_rate:.2%}) match")

            self.column_stats.append(
                {
                    "column": column,
                    "match_column": col_match,
                    "match_cnt": match_cnt,
                    "unequal_cnt": row_cnt - match_cnt,
                    "dtype1": str(col1_dtype),
                    "dtype2": str(col2_dtype),
                    "all_match": all(
                        (
                            col1_dtype == col2_dtype,
                            row_cnt == match_cnt,
                        )
                    ),
                    "max_diff": max_diff,
                    "null_diff": null_diff,
                }
            )

    def all_columns_match(self) -> bool:
        """Whether the columns all match in the dataframes"""
        return self.df1_unq_columns() == self.df2_unq_columns() == set()

    def all_rows_overlap(self) -> bool:
        """Whether the rows are all present in both dataframes

        Returns
        -------
        bool
            True if all rows in df1 are in df2 and vice versa (based on
            existence for join option)
        """
        return len(self.df1_unq_rows) == len(self.df2_unq_rows) == 0

    def count_matching_rows(self) -> int:
        """Count the number of rows match (on overlapping fields)

        Returns
        -------
        int
            Number of matching rows
        """
        conditions = []
        match_columns = []
        for column in self.intersect_columns():
            if column not in self.join_columns:
                match_columns.append(column + "_MATCH")
                conditions.append(f"{column}_MATCH = True")
        if len(conditions) > 0:
            match_columns_count = self.intersect_rows.filter(
                " and ".join(conditions)
            ).count()
        else:
            match_columns_count = 0
        return match_columns_count

    def intersect_rows_match(self) -> bool:
        """Check whether the intersect rows all match"""
        actual_length = self.intersect_rows.count()
        return self.count_matching_rows() == actual_length

    def matches(self, ignore_extra_columns: bool = False) -> bool:
        """Return True or False if the dataframes match.

        Parameters
        ----------
        ignore_extra_columns : bool
            Ignores any columns in one dataframe and not in the other.

        Returns
        -------
        bool
            True or False if the dataframes match.
        """
        if not ignore_extra_columns and not self.all_columns_match():
            return False
        elif not self.all_rows_overlap():
            return False
        elif not self.intersect_rows_match():
            return False
        else:
            return True

    def subset(self) -> bool:
        """Return True if dataframe 2 is a subset of dataframe 1.

        Dataframe 2 is considered a subset if all of its columns are in
        dataframe 1, and all of its rows match rows in dataframe 1 for the
        shared columns.

        Returns
        -------
        bool
            True if dataframe 2 is a subset of dataframe 1.
        """
        if not self.df2_unq_columns() == set():
            return False
        elif not len(self.df2_unq_rows) == 0:
            return False
        elif not self.intersect_rows_match():
            return False
        else:
            return True

    def sample_mismatch(
        self, column: str, sample_count: int = 10, for_display: bool = False
    ) -> "sp.DataFrame":
        """Returns a sample sub-dataframe which contains the identifying
        columns, and df1 and df2 versions of the column.

        Parameters
        ----------
        column : str
            The raw column name (i.e. without ``_df1`` appended)
        sample_count : int, optional
            The number of sample records to return.  Defaults to 10.
        for_display : bool, optional
            Whether this is just going to be used for display (overwrite the
            column names)

        Returns
        -------
        sp.DataFrame
            A sample of the intersection dataframe, containing only the
            "pertinent" columns, for rows that don't match on the provided
            column.
        """
        row_cnt = self.intersect_rows.count()
        col_match = self.intersect_rows.select(column + "_match")
        match_cnt = col_match.where(
            col(column + "_match") == True  # noqa: E712
        ).count()
        sample_count = min(sample_count, row_cnt - match_cnt)
        sample = (
            self.intersect_rows.where(col(column + "_match") == False)  # noqa: E712
            .drop(column + "_match")
            .limit(sample_count)
        )

        return_cols = self.join_columns + [
            column + "_" + self.df1_name,
            column + "_" + self.df2_name,
        ]
        to_return = sample.select(return_cols)

        if for_display:
            return to_return.toDF(
                *self.join_columns
                + [
                    column + " (" + self.df1_name + ")",
                    column + " (" + self.df2_name + ")",
                ]
            )
        return to_return

    def all_mismatch(self, ignore_matching_cols: bool = False) -> "sp.DataFrame":
        """All rows with any columns that have a mismatch. Returns all df1 and df2 versions of the columns and join
        columns.

        Parameters
        ----------
        ignore_matching_cols : bool, optional
            Whether showing the matching columns in the output or not. The default is False.

        Returns
        -------
        sp.DataFrame
            All rows of the intersection dataframe, containing any columns, that don't match.
        """
        match_list = []
        return_list = []
        for c in self.intersect_rows.columns:
            if c.endswith("_match"):
                orig_col_name = c[:-6]

                col_comparison = columns_equal(
                    self.intersect_rows,
                    orig_col_name + "_" + self.df1_name,
                    orig_col_name + "_" + self.df2_name,
                    c,
                    self.rel_tol,
                    self.abs_tol,
                    self.ignore_spaces,
                )

                if not ignore_matching_cols or (
                    ignore_matching_cols
                    and col_comparison.select(c)
                    .where(col(c) == False)  # noqa: E712
                    .count()
                    > 0
                ):
                    LOG.debug(f"Adding column {orig_col_name} to the result.")
                    match_list.append(c)
                    return_list.extend(
                        [
                            orig_col_name + "_" + self.df1_name,
                            orig_col_name + "_" + self.df2_name,
                        ]
                    )
                elif ignore_matching_cols:
                    LOG.debug(
                        f"Column {orig_col_name} is equal in df1 and df2. It will not be added to the result."
                    )

        mm_rows = self.intersect_rows.withColumn(
            "match_array", array(match_list)
        ).where(array_contains("match_array", False))

        for c in self.join_columns:
            mm_rows = mm_rows.withColumnRenamed(c + "_" + self.df1_name, c)

        return mm_rows.select(self.join_columns + return_list)

    def report(
        self,
        sample_count: int = 10,
        column_count: int = 10,
        html_file: Optional[str] = None,
    ) -> str:
        """Returns a string representation of a report.  The representation can
        then be printed or saved to a file.

        Parameters
        ----------
        sample_count : int, optional
            The number of sample records to return.  Defaults to 10.

        column_count : int, optional
            The number of columns to display in the sample records output.  Defaults to 10.

        html_file : str, optional
            HTML file name to save report output to. If ``None`` the file creation will be skipped.

        Returns
        -------
        str
            The report, formatted kinda nicely.
        """
        # Header
        report = render("header.txt")
        df_header = pd.DataFrame(
            {
                "DataFrame": [self.df1_name, self.df2_name],
                "Columns": [len(self.df1.columns), len(self.df2.columns)],
                "Rows": [self.df1.count(), self.df2.count()],
            }
        )
        report += df_header[["DataFrame", "Columns", "Rows"]].to_string()
        report += "\n\n"

        # Column Summary
        report += render(
            "column_summary.txt",
            len(self.intersect_columns()),
            len(self.df1_unq_columns()),
            len(self.df2_unq_columns()),
            self.df1_name,
            self.df2_name,
        )

        # Row Summary
        match_on = ", ".join(self.join_columns)
        report += render(
            "row_summary.txt",
            match_on,
            self.abs_tol,
            self.rel_tol,
            self.intersect_rows.count(),
            self.df1_unq_rows.count(),
            self.df2_unq_rows.count(),
            self.intersect_rows.count() - self.count_matching_rows(),
            self.count_matching_rows(),
            self.df1_name,
            self.df2_name,
            "Yes" if self._any_dupes else "No",
        )

        # Column Matching
        report += render(
            "column_comparison.txt",
            len([col for col in self.column_stats if col["unequal_cnt"] > 0]),
            len([col for col in self.column_stats if col["unequal_cnt"] == 0]),
            sum([col["unequal_cnt"] for col in self.column_stats]),
        )

        match_stats = []
        match_sample = []
        any_mismatch = False
        for column in self.column_stats:
            if not column["all_match"]:
                any_mismatch = True
                match_stats.append(
                    {
                        "Column": column["column"],
                        f"{self.df1_name} dtype": column["dtype1"],
                        f"{self.df2_name} dtype": column["dtype2"],
                        "# Unequal": column["unequal_cnt"],
                        "Max Diff": column["max_diff"],
                        "# Null Diff": column["null_diff"],
                    }
                )
                if column["unequal_cnt"] > 0:
                    match_sample.append(
                        self.sample_mismatch(
                            column["column"], sample_count, for_display=True
                        )
                    )

        if any_mismatch:
            report += "Columns with Unequal Values or Types\n"
            report += "------------------------------------\n"
            report += "\n"
            df_match_stats = pd.DataFrame(match_stats)
            df_match_stats.sort_values("Column", inplace=True)
            # Have to specify again for sorting
            report += df_match_stats[
                [
                    "Column",
                    f"{self.df1_name} dtype",
                    f"{self.df2_name} dtype",
                    "# Unequal",
                    "Max Diff",
                    "# Null Diff",
                ]
            ].to_string()
            report += "\n\n"

            if sample_count > 0:
                report += "Sample Rows with Unequal Values\n"
                report += "-------------------------------\n"
                report += "\n"
                for sample in match_sample:
                    report += sample.toPandas().to_string()
                    report += "\n\n"

        if min(sample_count, self.df1_unq_rows.count()) > 0:
            report += (
                f"Sample Rows Only in {self.df1_name} (First {column_count} Columns)\n"
            )
            report += (
                f"---------------------------------------{'-' * len(self.df1_name)}\n"
            )
            report += "\n"
            columns = self.df1_unq_rows.columns[:column_count]
            unq_count = min(sample_count, self.df1_unq_rows.count())
            report += (
                self.df1_unq_rows.limit(unq_count)
                .select(columns)
                .toPandas()
                .to_string()
            )
            report += "\n\n"

        if min(sample_count, self.df2_unq_rows.count()) > 0:
            report += (
                f"Sample Rows Only in {self.df2_name} (First {column_count} Columns)\n"
            )
            report += (
                f"---------------------------------------{'-' * len(self.df2_name)}\n"
            )
            report += "\n"
            columns = self.df2_unq_rows.columns[:column_count]
            unq_count = min(sample_count, self.df2_unq_rows.count())
            report += (
                self.df2_unq_rows.limit(unq_count)
                .select(columns)
                .toPandas()
                .to_string()
            )
            report += "\n\n"

        if html_file:
            html_report = report.replace("\n", "<br>").replace(" ", "&nbsp;")
            html_report = f"<pre>{html_report}</pre>"
            with open(html_file, "w") as f:
                f.write(html_report)

        return report


def render(filename: str, *fields: Union[int, float, str]) -> str:
    """Renders out an individual template.  This basically just reads in a
    template file, and applies ``.format()`` on the fields.

    Parameters
    ----------
    filename : str
        The file that contains the template.  Will automagically prepend the
        templates directory before opening
    fields : list
        Fields to be rendered out in the template

    Returns
    -------
    str
        The fully rendered out file.
    """
    this_dir = os.path.dirname(os.path.realpath(__file__))
    with open(os.path.join(this_dir, "templates", filename)) as file_open:
        return file_open.read().format(*fields)


def columns_equal(
    dataframe: sp.DataFrame,
    col_1: str,
    col_2: str,
    col_match: str,
    rel_tol: float = 0,
    abs_tol: float = 0,
    ignore_spaces: bool = False,
) -> sp.DataFrame:
    """Compares two columns from a dataframe, returning a True/False series,
    with the same index as column 1.

    - Two nulls (np.nan) will evaluate to True.
    - A null and a non-null value will evaluate to False.
    - Numeric values will use the relative and absolute tolerances.
    - Decimal values (decimal.Decimal) will attempt to be converted to floats
      before comparing
    - Non-numeric values (i.e. where np.isclose can't be used) will just
      trigger True on two nulls or exact matches.

    Parameters
    ----------
    dataframe: sp.DataFrame
        DataFrame to do comparison on
    col_1 : str
        The first column to look at
    col_2 : str
        The second column
    col_match : str
        The matching column denoting if the compare was a match or not
    rel_tol : float, optional
        Relative tolerance
    abs_tol : float, optional
        Absolute tolerance
    ignore_spaces : bool, optional
        Flag to strip whitespace (including newlines) from string columns

    Returns
    -------
    sp.DataFrame
        A column of boolean values are added.  True == the values match, False == the
        values don't match.
    """
    base_dtype, compare_dtype = _get_column_dtypes(dataframe, col_1, col_2)
    if _is_comparable(base_dtype, compare_dtype):
        if (base_dtype in NUMERIC_SPARK_TYPES) and (
            compare_dtype in NUMERIC_SPARK_TYPES
        ):  # numeric tolerance comparison
            dataframe = dataframe.withColumn(
                col_match,
                when(
                    (col(col_1).eqNullSafe(col(col_2)))
                    | (
                        abs(col(col_1) - col(col_2))
                        <= lit(abs_tol) + (lit(rel_tol) * abs(col(col_2)))
                    ),
                    # corner case of col1 != NaN and col2 == Nan returns True incorrectly
                    when(
                        (is_null(col(col_1)) == False)  # noqa: E712
                        & (is_null(col(col_2)) == True),  # noqa: E712
                        lit(False),
                    ).otherwise(lit(True)),
                ).otherwise(lit(False)),
            )
        else:  # non-numeric comparison
            if ignore_spaces:
                when_clause = trim(col(col_1)).eqNullSafe(trim(col(col_2)))
            else:
                when_clause = col(col_1).eqNullSafe(col(col_2))

            dataframe = dataframe.withColumn(
                col_match,
                when(when_clause, lit(True)).otherwise(lit(False)),
            )
    else:
        LOG.debug(
            "Skipping {}({}) and {}({}), columns are not comparable".format(
                col_1, base_dtype, col_2, compare_dtype
            )
        )
        dataframe = dataframe.withColumn(col_match, lit(False))
    return dataframe


def get_merged_columns(
    original_df: sp.DataFrame, merged_df: sp.DataFrame, suffix: str
) -> List[str]:
    """Gets the columns from an original dataframe, in the new merged dataframe

    Parameters
    ----------
    original_df : Pandas.DataFrame
        The original, pre-merge dataframe
    merged_df : Pandas.DataFrame
        Post-merge with another dataframe, with suffixes added in.
    suffix : str
        What suffix was used to distinguish when the original dataframe was
        overlapping with the other merged dataframe.
    """
    columns = []
    for col in original_df.columns:
        if col in merged_df.columns:
            columns.append(col)
        elif f"{col}_{suffix}" in merged_df.columns:
            columns.append(f"{col}_{suffix}")
        else:
            raise ValueError("Column not found: %s", col)
    return columns


def temp_column_name(*dataframes: sp.DataFrame) -> str:
    """Gets a temp column name that isn't included in columns of any dataframes

    Parameters
    ----------
    dataframes : list of Pandas.DataFrame
        The DataFrames to create a temporary column name for

    Returns
    -------
    str
        String column name that looks like '_temp_x' for some integer x
    """
    i = 0
    while True:
        temp_column = f"_temp_{i}"
        unique = True
        for dataframe in dataframes:
            if temp_column in dataframe.columns:
                i += 1
                unique = False
        if unique:
            return temp_column


def calculate_max_diff(
    dataframe: "sp.DataFrame", col_1: str, col_2: str, type1: str, type2: str
) -> float:
    """Get a maximum difference between two columns

    Parameters
    ----------
    dataframe: sp.DataFrame
        DataFrame to do comparison on
    col_1 : str
        The first column to look at
    col_2 : str
        The second column
    type_1: str
        The type of the first column
    type_2: str
        The type of the second column

    Returns
    -------
    float
        max diff
    """
    if not (type1 in NUMERIC_SPARK_TYPES and type2 in NUMERIC_SPARK_TYPES):
        return 0

    diff = dataframe.select(
        (col(col_1).astype("float") - col(col_2).astype("float")).alias("diff")
    )
    abs_diff = diff.select(abs(col("diff")).alias("abs_diff"))
    max_diff: float = (
        abs_diff.where(is_null(col("abs_diff")) == False)  # noqa: E712
        .agg({"abs_diff": "max"})
        .collect()[0][0]
    )

    if pd.isna(max_diff) or pd.isnull(max_diff) or max_diff is None:
        return 0
    else:
        return max_diff


def calculate_null_diff(dataframe: "sp.DataFrame", col_1: str, col_2: str) -> int:
    """Get the null differences between two columns

    Parameters
    ----------
    dataframe: sp.DataFrame
        DataFrame to do comparison on
    col_1 : str
        The first column to look at
    col_2 : str
        The second column

    Returns
    -------
    int
        null diff
    """
    nulls_df = dataframe.withColumn(
        "col_1_null",
        when(col(col_1).isNull() == True, lit(True)).otherwise(  # noqa: E712
            lit(False)
        ),
    )
    nulls_df = nulls_df.withColumn(
        "col_2_null",
        when(col(col_2).isNull() == True, lit(True)).otherwise(  # noqa: E712
            lit(False)
        ),
    ).select(["col_1_null", "col_2_null"])

    # (not a and b) or (a and not b)
    null_diff = nulls_df.where(
        ((col("col_1_null") == False) & (col("col_2_null") == True))  # noqa: E712
        | ((col("col_1_null") == True) & (col("col_2_null") == False))  # noqa: E712
    ).count()

    if pd.isna(null_diff) or pd.isnull(null_diff) or null_diff is None:
        return 0
    else:
        return null_diff


def _generate_id_within_group(
    dataframe: "sp.DataFrame", join_columns: List[str], order_column_name: str
) -> "sp.DataFrame":
    """Generate an ID column that can be used to deduplicate identical rows.  The series generated
    is the order within a unique group, and it handles nulls. Requires a ``__index`` column.

    Parameters
    ----------
    dataframe : sp.DataFrame
        The dataframe to operate on
    join_columns : list
        List of strings which are the join columns
    order_column_name: str
        The name of the ``row_number`` column name

    Returns
    -------
    sp.DataFrame
        Original dataframe with the ID column that's unique in each group
    """
    default_value = "DATACOMPY_NULL"
    null_check = False
    default_check = False
    for c in join_columns:
        if dataframe.where(col(c).isNull()).limit(1).collect():
            null_check = True
            break
    for c in [
        column for column, type in dataframe[join_columns].dtypes if "string" in type
    ]:
        if dataframe.where(col(c).isin(default_value)).limit(1).collect():
            default_check = True
            break

    if null_check:
        if default_check:
            raise ValueError(f"{default_value} was found in your join columns")

        return (
            dataframe.select(
                *(col(c).cast("string").alias(c) for c in join_columns + ["__index"])
            )
            .fillna(default_value)
            .withColumn(
                order_column_name,
                row_number().over(Window.orderBy("__index").partitionBy(join_columns))
                - 1,
            )
            .select(["__index", order_column_name])
        )
    else:
        return (
            dataframe.select(join_columns + ["__index"])
            .withColumn(
                order_column_name,
                row_number().over(Window.orderBy("__index").partitionBy(join_columns))
                - 1,
            )
            .select(["__index", order_column_name])
        )


def _get_column_dtypes(
    dataframe: "sp.DataFrame", col_1: "str", col_2: "str"
) -> tuple[str, str]:
    """Get the dtypes of two columns

    Parameters
    ----------
    dataframe: sp.DataFrame
        DataFrame to do comparison on
    col_1 : str
        The first column to look at
    col_2 : str
        The second column

    Returns
    -------
    Tuple(str, str)
        Tuple of base and compare datatype
    """
    base_dtype = [d[1] for d in dataframe.dtypes if d[0] == col_1][0]
    compare_dtype = [d[1] for d in dataframe.dtypes if d[0] == col_2][0]
    return base_dtype, compare_dtype


def _is_comparable(type1: str, type2: str) -> bool:
    """Checks if two Spark data types can be safely compared.

    Two data types are considered comparable if any of the following apply:
        1. Both data types are the same
        2. Both data types are numeric

    Parameters
    ----------
    type1 : str
        A string representation of a Spark data type
    type2 : str
        A string representation of a Spark data type

    Returns
    -------
    bool
        True if both data types are comparable
    """
    return (
        type1 == type2
        or (type1 in NUMERIC_SPARK_TYPES and type2 in NUMERIC_SPARK_TYPES)
        or ({type1, type2} == {"string", "timestamp"})
        or ({type1, type2} == {"string", "date"})
    )
