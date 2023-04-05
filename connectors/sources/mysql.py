#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
"""MySQL source module responsible to fetch documents from MySQL"""
import re

import aiomysql

from connectors.filtering.validation import (
    AdvancedRulesValidator,
    SyncRuleValidationResult,
)
from connectors.logger import logger
from connectors.source import BaseDataSource, ConfigurableFieldValueError
from connectors.sources.generic_database import (
    WILDCARD,
    Queries,
    configured_tables,
    is_wildcard,
)
from connectors.utils import CancellableSleeps, RetryStrategy, retryable, ssl_context

SPLIT_BY_COMMA_OUTSIDE_BACKTICKS_PATTERN = re.compile(r"`(?:[^`]|``)+`|\w+")

MAX_POOL_SIZE = 10
DEFAULT_FETCH_SIZE = 50
RETRIES = 3
RETRY_INTERVAL = 2
DEFAULT_SSL_ENABLED = False
DEFAULT_SSL_CA = ""


def parse_tables_string_to_list_of_tables(tables_string):
    if tables_string is None or len(tables_string) == 0:
        return []

    return SPLIT_BY_COMMA_OUTSIDE_BACKTICKS_PATTERN.findall(tables_string)


def format_list(list_):
    return ", ".join(list_)


class MySQLQueries(Queries):
    def __init__(self, database):
        self.database = database

    def all_tables(self, **kwargs):
        return f"SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = '{self.database}'"

    def table_primary_key(self, table):
        return f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = '{self.database}' AND TABLE_NAME = '{table}' AND COLUMN_KEY = 'PRI'"

    def table_data(self, table):
        return f"SELECT * FROM {self.database}.{table}"

    def table_last_update_time(self, table):
        return f"SELECT UPDATE_TIME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = '{self.database}' AND TABLE_NAME = '{table}'"

    def columns(self, table):
        return f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = '{self.database}' AND TABLE_NAME = '{table}'"

    def ping(self):
        pass

    def table_data_count(self, **kwargs):
        pass

    def all_schemas(self):
        pass


class MySQLAdvancedRulesValidator(AdvancedRulesValidator):
    def __init__(self, source):
        self.source = source

    async def validate(self, advanced_rules):
        return await self._remote_validation(advanced_rules)

    @retryable(
        retries=RETRIES,
        interval=RETRY_INTERVAL,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
    )
    async def _remote_validation(self, advanced_rules):
        await self.source.ping()

        tables = set(
            map(
                lambda table: table[0],
                await self.source.fetch_all_tables(),
            )
        )
        tables_to_filter = set(advanced_rules.keys())

        missing_tables = tables_to_filter - tables

        if len(missing_tables) > 0:
            return SyncRuleValidationResult(
                SyncRuleValidationResult.ADVANCED_RULES,
                is_valid=False,
                validation_message=f"Tables not found or inaccessible: {format_list(sorted(list(missing_tables)))}.",
            )

        return SyncRuleValidationResult.valid_result(
            SyncRuleValidationResult.ADVANCED_RULES
        )


class MySQLClient:
    def __init__(
        self,
        host,
        port,
        user,
        password,
        ssl_enabled,
        ssl_certificate,
        database=None,
        max_pool_size=MAX_POOL_SIZE,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.max_pool_size = max_pool_size
        self.ssl_enabled = ssl_enabled
        self.ssl_certificate = ssl_certificate
        self.queries = MySQLQueries(self.database)
        self.connection_pool = None
        self.connection = None

    async def __aenter__(self):
        connection_string = {
            "host": self.host,
            "port": int(self.port),
            "user": self.user,
            "password": self.password,
            "db": self.database,
            "maxsize": self.max_pool_size,
            "ssl": ssl_context(certificate=self.ssl_certificate)
            if self.ssl_enabled
            else None,
        }
        self.connection_pool = await aiomysql.create_pool(**connection_string)
        self.connection = await self.connection_pool.acquire()

        return self

    async def __aexit__(self, exception_type, exception_value, exception_traceback):
        self.connection_pool.release(self.connection)
        self.connection_pool.close()
        await self.connection_pool.wait_closed()

    @retryable(
        retries=RETRIES,
        interval=RETRY_INTERVAL,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
    )
    async def get_all_table_names(self):
        async with self.connection.cursor(aiomysql.cursors.SSCursor) as cursor:
            await cursor.execute(self.queries.all_tables())
            return list(map(lambda table: table[0], await cursor.fetchall()))

    async def ping(self):
        try:
            await self.connection.ping()
            logger.info("Successfully connected to the MySQL Server.")
        except Exception:
            logger.exception("Error while connecting to the MySQL Server.")
            raise

    @retryable(
        retries=RETRIES,
        interval=RETRY_INTERVAL,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
    )
    async def get_column_names(self, table, query=None):
        if query is None:
            # fetch all columns on default
            query = self.queries.columns(table)

        async with self.connection.cursor(aiomysql.cursors.SSCursor) as cursor:
            await cursor.execute(query)

            return [f"{table}_{column[0]}" for column in cursor.description]

    @retryable(
        retries=RETRIES,
        interval=RETRY_INTERVAL,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
    )
    async def get_last_update_time(self, table):
        async with self.connection.cursor(aiomysql.cursors.SSCursor) as cursor:
            await cursor.execute(self.queries.table_last_update_time(table))

            result = await cursor.fetchone()

            if result is not None:
                return result[0]

            return None


class MySqlDataSource(BaseDataSource):
    """MySQL"""

    name = "MySQL"
    service_type = "mysql"

    def __init__(self, configuration):
        """Set up the connection to the MySQL server.

        Args:
            configuration (DataSourceConfiguration): Object  of DataSourceConfiguration class.
        """
        super().__init__(configuration=configuration)
        self._sleeps = CancellableSleeps()
        self.retry_count = self.configuration["retry_count"]
        self.ssl_enabled = self.configuration["ssl_enabled"]
        self.certificate = self.configuration["ssl_ca"]
        self.database = self.configuration["database"]
        self.tables = self.configuration["tables"]
        self.queries = MySQLQueries(self.database)

    @classmethod
    def get_default_configuration(cls):
        """Get the default configuration for MySQL server

        Returns:
            dictionary: Default configuration
        """
        return {
            "host": {
                "label": "Host",
                "order": 1,
                "type": "str",
                "value": "127.0.0.1",
            },
            "port": {
                "display": "numeric",
                "label": "Port",
                "order": 2,
                "type": "int",
                "value": 3306,
            },
            "user": {
                "label": "Username",
                "order": 3,
                "type": "str",
                "value": "root",
            },
            "password": {
                "label": "Password",
                "order": 4,
                "sensitive": True,
                "type": "str",
                "value": "changeme",
            },
            "database": {
                "label": "Database",
                "order": 5,
                "type": "str",
                "value": "customerinfo",
            },
            "tables": {
                "display": "textarea",
                "label": "Comma-separated list of tables",
                "order": 6,
                "type": "list",
                "value": WILDCARD,
            },
            "ssl_enabled": {
                "display": "toggle",
                "label": "Enable SSL verification",
                "order": 7,
                "type": "bool",
                "value": DEFAULT_SSL_ENABLED,
            },
            "ssl_ca": {
                "label": "SSL certificate",
                "order": 8,
                "type": "str",
                "value": DEFAULT_SSL_CA,
            },
            "fetch_size": {
                "default_value": DEFAULT_FETCH_SIZE,
                "display": "numeric",
                "label": "Number of rows fetched per request",
                "order": 9,
                "required": False,
                "type": "int",
                "ui_restrictions": ["advanced"],
                "value": DEFAULT_FETCH_SIZE,
            },
            "retry_count": {
                "default_value": RETRIES,
                "display": "numeric",
                "label": "Maximum retries per request",
                "order": 10,
                "required": False,
                "type": "int",
                "ui_restrictions": ["advanced"],
                "value": RETRIES,
            },
        }

    def _mysql_client(self):
        return MySQLClient(
            host=self.configuration["host"],
            port=self.configuration["port"],
            user=self.configuration["user"],
            password=self.configuration["password"],
            database=self.configuration["database"],
            ssl_enabled=self.configuration["ssl_enabled"],
            ssl_certificate=self.configuration["ssl_ca"],
        )

    def advanced_rules_validators(self):
        return [MySQLAdvancedRulesValidator(self)]

    async def close(self):
        self._sleeps.cancel()

    async def validate_config(self):
        """Validates whether user input is empty or not for configuration fields and validate type for port.
        Also validate, if the configured database and the configured tables are present and accessible by the configured user.

        Raises:
            Exception: Configured keys can't be empty
        """
        connection_fields = ["host", "port", "user", "password", "database", "tables"]
        empty_connection_fields = []
        for field in connection_fields:
            if self.configuration[field] == "":
                empty_connection_fields.append(field)

        if empty_connection_fields:
            raise ConfigurableFieldValueError(
                f"Configured keys: {empty_connection_fields} can't be empty."
            )

        if (
            isinstance(self.configuration["port"], str)
            and not self.configuration["port"].isnumeric()
        ):
            raise ConfigurableFieldValueError("Configured port has to be an integer.")

        if self.ssl_enabled and (self.certificate == "" or self.certificate is None):
            raise ConfigurableFieldValueError("SSL certificate must be configured.")

        await self._remote_validation()

    @retryable(
        retries=RETRIES,
        interval=RETRY_INTERVAL,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
    )
    async def _remote_validation(self):
        async with self._mysql_client() as client:
            async with client.connection.cursor() as cursor:
                await self._validate_database_accessible(cursor)
                await self._validate_tables_accessible(cursor)

    async def _validate_database_accessible(self, cursor):
        try:
            await cursor.execute(f"USE {self.database};")
        except aiomysql.Error:
            raise ConfigurableFieldValueError(
                f"The database '{self.database}' is either not present or not accessible for the user '{self.configuration['user']}'."
            )

    async def _validate_tables_accessible(self, cursor):
        non_accessible_tables = []
        tables_to_validate = await self.get_tables_to_fetch()

        for table in tables_to_validate:
            try:
                await cursor.execute(f"SELECT 1 FROM {table} LIMIT 1;")
            except aiomysql.Error:
                non_accessible_tables.append(table)

        if len(non_accessible_tables) > 0:
            raise ConfigurableFieldValueError(
                f"The tables '{format_list(non_accessible_tables)}' are either not present or not accessible for user '{self.configuration['user']}'."
            )

    async def ping(self):
        async with self._mysql_client() as client:
            await client.ping()

    async def _connect(self, query, fetch_many=False, **query_kwargs):
        """Executes the passed query on the MySQL server.

        Args:
            query (str): MySql query to be executed.
            query_kwargs (dict): Query kwargs to format the query.
            fetch_many (boolean): Should use fetchmany to fetch the response.

        Yields:
            list: Column names and query response
        """

        formatted_query = query.format(**query_kwargs)
        size = self.configuration["fetch_size"]
        retry = 1
        yield_once = True

        rows_fetched = 0
        cursor_position = 0

        async with self._mysql_client() as client:
            while retry <= self.retry_count:
                try:
                    async with client.connection.cursor(
                        aiomysql.cursors.SSCursor
                    ) as cursor:
                        await cursor.execute(formatted_query)

                        if fetch_many:
                            # sending back column names only once
                            if yield_once:
                                yield [
                                    f"{query_kwargs['table']}_{column[0]}"
                                    for column in cursor.description
                                ]
                                yield_once = False

                            # setting cursor position where it was failed
                            if cursor_position:
                                await cursor.scroll(cursor_position, mode="absolute")

                            while True:
                                rows = await cursor.fetchmany(size=size)
                                rows_length = len(rows)

                                # resetting cursor position & retry to 0 for next batch
                                if cursor_position:
                                    cursor_position = retry = 0

                                if not rows_length:
                                    break

                                for row in rows:
                                    yield row

                                rows_fetched += rows_length
                                await self._sleeps.sleep(0)
                        else:
                            yield await cursor.fetchall()
                        break
                except IndexError as exception:
                    logger.exception(
                        f"None of responses fetched from {rows_fetched} rows. Exception: {exception}"
                    )
                    break
                except Exception as exception:
                    logger.warning(
                        f"Retry count: {retry} out of {self.retry_count}. Exception: {exception}"
                    )
                    if retry == self.retry_count:
                        raise exception
                    cursor_position = rows_fetched
                    await self._sleeps.sleep(RETRY_INTERVAL**retry)
                    retry += 1

    async def fetch_all_tables(self):
        return await anext(self._connect(query=self.queries.all_tables()))

    async def fetch_rows_for_table(self, table=None, query=None):
        """Fetches all the rows from all the tables of the database.

        Args:
            table (str): Name of the table to fetch from
            query (str): MySQL query

        Yields:
            Dict: Row document to index
        """
        if table is not None:
            async for row in self.fetch_documents(table=table, query=query):
                yield row
        else:
            logger.warning(
                f"Fetched 0 rows for the table: {table}. As table has no rows."
            )

    async def fetch_rows_from_tables(self, tables):
        """Fetches all the rows from all the tables of the database.

        Yields:
            Dict: Row document to index
        """
        if tables:
            for table in tables:
                logger.debug(f"Found table: {table} in database: {self.database}.")

                async for row in self.fetch_rows_for_table(
                    table=table,
                    query=self.queries.table_data(table),
                ):
                    yield row
        else:
            logger.warning(
                f"Fetched 0 tables for database: {self.database}. As database has no tables."
            )

    async def fetch_documents(self, table, query=None):
        """Fetches all the table entries and format them in Elasticsearch documents

        Args:
            table (str): Name of table
            query (str): Query to fetch data from a table

        Yields:
            Dict: Document to be indexed
        """

        primary_key_columns = await self._get_primary_key_columns(table)

        if not primary_key_columns:
            logger.warning(
                f"Skipping {table} table from database {self.database} since no primary key is associated with it. Assign primary key to the table to index it in the next sync interval."
            )
            return

        last_update_time = await anext(
            self._connect(
                query=self.queries.table_last_update_time(table),
                table=table,
            )
        )
        last_update_time = last_update_time[0][0]

        table_rows = self._connect(query=query, fetch_many=True, table=table)
        column_names = await anext(table_rows)

        async for row in table_rows:
            row = dict(zip(column_names, row))
            row.update(
                {
                    "_id": self._generate_id(table, row, primary_key_columns),
                    "_timestamp": last_update_time,
                    "Table": table,
                }
            )
            yield self.serialize(doc=row)

    def _generate_id(self, table, row, primary_key_columns):
        keys_value = ""
        for key in primary_key_columns:
            keys_value += f"{row.get(key)}_" if row.get(key) else ""

        return f"{table}_{keys_value}"

    async def _get_primary_key_columns(self, table):
        primary_key = await anext(
            self._connect(query=self.queries.table_primary_key(table), table=table)
        )

        columns = []
        for column_name in primary_key:
            columns.append(f"{table}_{column_name[0]}")

        return columns

    async def get_docs(self, filtering=None):
        """Executes the logic to fetch tables and rows in async manner.

        Yields:
            dictionary: Row dictionary containing meta-data of the row.
        """
        if filtering and filtering.has_advanced_rules():
            advanced_rules = filtering.get_advanced_rules()

            for table in advanced_rules:
                query = advanced_rules.get(table)
                logger.debug(
                    f"Fetching rows from table '{table}' in database '{self.database}' with a custom query."
                )
                async for row in self.fetch_rows_for_table(
                    table=table,
                    query=query,
                ):
                    yield row, None
                await self._sleeps.sleep(0)
        else:
            tables_to_fetch = await self.get_tables_to_fetch()

            async for row in self.fetch_rows_from_tables(tables_to_fetch):
                yield row, None
            await self._sleeps.sleep(0)

    async def get_tables_to_fetch(self):
        tables = configured_tables(self.tables)

        def table_name(table):
            return table[0]

        return (
            map(lambda table: table_name(table), await self.fetch_all_tables())
            if is_wildcard(tables)
            else tables
        )
