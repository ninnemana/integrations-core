# (C) Datadog, Inc. 2019
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import json
import os
import re

from six import iteritems

from datadog_checks.base import AgentCheck, ensure_unicode
from datadog_checks.base.errors import CheckException

from .common import ALLOWED_METRICS, COUNT_METRICS, GAUGE_METRICS, MONOTONIC_COUNTER_METRICS
from .utils import get_fqdn, get_stream_id_for_topic

try:
    # The `confluent_kafka` library here is the one made by mapr
    import confluent_kafka as ck
except ImportError:
    ck = None


DEFAULT_STREAM_PATH = "/var/mapr/mapr.monitoring/metricstreams"
METRICS_SUBMITTED_METRIC_NAME = "mapr.metrics.submitted"


class MaprCheck(AgentCheck):
    def __init__(self, name, init_config, instances):
        super(MaprCheck, self).__init__(name, init_config, instances)
        self._conn = None
        self.hostname = self.instance.get('hostname', get_fqdn())
        self.topic_path = "{stream_path}/{stream_id}:{topic_name}".format(
            stream_path=self.instance.get('stream_path', DEFAULT_STREAM_PATH),
            stream_id=get_stream_id_for_topic(self.hostname),
            topic_name=self.hostname,
        )
        self.allowed_metrics = [re.compile(w) for w in self.instance.get('metrics', [])]
        self.base_tags = self.instance.get('tags', [])
        self.is_first_check_run = True

        auth_ticket = self.instance.get('ticket_location')
        if auth_ticket:
            os.environ['MAPR_TICKETFILE_LOCATION'] = auth_ticket
        elif not os.environ.get('MAPR_TICKETFILE_LOCATION'):
            self.log.info("MAPR_TICKETFILE_LOCATION environment variable not set, this may cause authentication issues")

    def check(self, _):
        if ck is None:
            raise CheckException(
                "confluent_kafka was not imported correctly, make sure the library is installed and that you've"
                "set LD_LIBRARY_PATH correctly. Please refer to datadog documentation for more details."
            )

        conn = self.get_connection()

        submitted_metrics_count = 0

        while True:
            msg = conn.poll(timeout=0.5)
            if msg is None:
                # Timed out, no more messages
                break

            if msg.error() is None:
                # Metric received
                try:
                    metric = json.loads(ensure_unicode(msg.value()))[0]
                    metric_name = metric['metric']
                    if self.should_collect_metric(metric_name):
                        # Will sometimes submit the same metric multiple time, but because it's only
                        # gauges and monotonic_counter that's fine.
                        self.submit_metric(metric)
                        submitted_metrics_count += 1
                except Exception as e:
                    self.log.warning("Received unexpected message %s, wont be processed", msg.value())
                    self.log.exception(e)
            elif msg.error().code() != ck.KafkaError._PARTITION_EOF:
                # Real error happened
                raise CheckException(msg.error())

        if not submitted_metrics_count and not self.is_first_check_run:
            self.log.error("No metric to fetch in topic {}. Double-check the topic name and path", self.topic_path)

        self.gauge(METRICS_SUBMITTED_METRIC_NAME, submitted_metrics_count, self.base_tags)
        self.is_first_check_run = False

    def get_connection(self):
        if self._conn:
            return self._conn

        self._conn = ck.Consumer(
            {
                "group.id": "dd-agent",  # uniquely identify this consumer
                "enable.auto.commit": False  # important, we don't need to store the offset for this consumer,
                # and if we do it just once the mapr library has a bug which prevents reading from the head
            }
        )
        self._conn.subscribe([self.topic_path])
        return self._conn

    def should_collect_metric(self, metric_name):
        if metric_name not in ALLOWED_METRICS:
            # Metric is not part of datadog allowed list
            return False
        if not self.allowed_metrics:
            # No filter specified, allow everything
            return True

        for reg in self.allowed_metrics:
            if re.match(reg, metric_name):
                # Metric matched one pattern
                return True

        self.log.debug("Ignoring non whitelisted metric: %s", metric_name)
        return False

    def submit_metric(self, metric):
        metric_name = metric['metric']
        tags = self.base_tags + ["{}:{}".format(k, v) for k, v in iteritems(metric['tags'])]

        if 'buckets' in metric:
            for bounds, value in metric['buckets'].items():
                lower, upper = bounds.split(',')
                self.submit_histogram_bucket(
                    metric_name, value, int(lower), int(upper), monotonic=True, hostname=self.hostname, tags=tags
                )
        else:
            if metric_name in GAUGE_METRICS:
                self.gauge(metric_name, metric['value'], tags=tags)
            elif metric_name in MONOTONIC_COUNTER_METRICS:
                self.monotonic_count(metric_name, metric['value'], tags=tags)
            elif metric_name in COUNT_METRICS:
                self.count(metric_name, metric['value'], tags=tags)
