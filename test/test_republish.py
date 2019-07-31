"""This file contains tests expected behaviour around republishing."""

import datetime
import os
from subprocess import call, Popen, PIPE
import unittest

import apel.db.apeldb
import apel.db.loader.record_factory
import apel.db.records


class TestRepublish(unittest.TestCase):
    """This class contains tests for expected behaviour around republishing."""

    def setUp(self):
        """Set up a database and load schema files."""

        # Set up a database.
        query = ("DROP DATABASE IF EXISTS apel_unittest;"
                 "CREATE DATABASE apel_unittest;")

        call(["mysql", "-u", "root", "-e", query])

        # Build a list of schema files for later loading.
        # For now, only add the cloud schema file.
        schema_path_list = []
        schema_path_list.append(
            os.path.abspath(
                os.path.join("..", "schemas", "cloud.sql")
            )
        )

        # Load each schema in turn.
        for schema_path in schema_path_list:
            with open(schema_path) as schema_file:
                call(
                    ["mysql", "-u", "root", "apel_unittest"],
                    stdin=schema_file
                )

    def test_cloud_republish(self):
        """Test the most recently processed cloud record per month/VM is the one saved."""
        database = apel.db.apeldb.ApelDb(
            "mysql", "localhost", 3306, "root", "", "apel_unittest"
        )

        # This will be used to generate records that only differ by the wall
        # duration for later simulated (re)publishing.
        # They have been crafted so that:
        # - the corresponding records generated produce MeasurementTimes in
        #   same month.
        # - they test decreasing and increasing wall durations during
        #   republishing.

        wall_duration_list = [1582, 158, 851, 200]

        for wall_duration in wall_duration_list:
            # Create a record object
            record = apel.db.records.cloud.CloudRecord()
            record._record_content = {
                "VMUUID": "1559818218-Site1-VM12345",
                "SiteName": "Site1",
                "Status": "started",
                "StartTime": datetime.datetime.fromtimestamp(1559818218),
                "WallDuration": wall_duration,
            }

            # Load the record.
            database.load_records([record], source="testDN")

            # Create a timedelta object from the WallDuration for later
            # datetime addition.
            wall_delta = datetime.timedelta(
                seconds=record._record_content["WallDuration"]
            )

            # Calculate an expected MeasurementTime.
            expected_measurement_time = (
                record._record_content["StartTime"] + wall_delta
            )

            # Now check the database to see which record has been saved.
            self._check_measurement_time_equals(expected_measurement_time)

    def _check_measurement_time_equals(self, expected_measurement_time):
        """
        Check MeasurementTime in database is what we would expect.

        This method assumes there is only one record in VCloudRecords.
        """
        query = ("SELECT MeasurementTime FROM VCloudRecords;")
        mysql_process = Popen(
            ["mysql", "-N", "-u", "root", "apel_unittest", "-e", query],
            stdin=PIPE, stdout=PIPE, stderr=PIPE
        )
        mysql_process.wait()

        # Extract the actual result from the MySQL query.
        saved_measurement_time = datetime.datetime.strptime(
            mysql_process.communicate()[0], "%Y-%m-%d %H:%M:%S\n"
        )

        self.assertEqual(saved_measurement_time, expected_measurement_time)


if __name__ == "__main__":
    unittest.main()
