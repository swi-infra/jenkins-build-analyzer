import urllib3
import xml.etree.ElementTree as ET
import json
import re
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

pool_manager = urllib3.PoolManager(timeout=30.0)


def get_human_time(milliseconds):

    if milliseconds is None:
        milliseconds = 0

    try:
        d = datetime(1, 1, 1) + timedelta(milliseconds=int(milliseconds))
    except OverflowError as ex:
        logger.error("Unable to convert %s ms to human time: %s", milliseconds, ex)
        raise ex
    time = [d.day - 1, d.hour, d.minute, d.second + (milliseconds % 1000) / 1000.0]
    time_suffix = ["d", "h", "m", "s"]

    val = []
    for i in range(len(time)):
        if time[i] > 0:
            if time_suffix[i] == "s":
                s = "%.1f%s" % (time[i], time_suffix[i])
            else:
                s = "%d%s" % (time[i], time_suffix[i])
            val.append(s)
    return " ".join(val)


class BuildNotFoundException(Exception):
    def __init__(self, build_info):

        super(BuildNotFoundException, self).__init__(
            "Job %s#%s not found" % (build_info.job_name, build_info.build_number)
        )


class BuildSection:
    def __init__(self, name, section_type=None):
        self.name = name
        self.__type = section_type
        self.parent = None
        self.children = []
        self.start = None
        self.end = None

    def __str__(self):
        info = self.name
        if self.type:
            info += " (%s)" % self.type
        info += " %s" % get_human_time(self.duration)
        return info

    @property
    def type(self):
        if self.__type:
            return self.__type

        if self.parent:
            return self.parent.type

        return None

    @property
    def duration(self):
        if not self.start or not self.end:
            return 0

        assert self.end >= self.start

        return self.end - self.start  # in us

    @property
    def parents_cnt(self):
        cnt = 0
        if self.parent is not None:
            cnt = 1 + self.parent.parents_cnt
        return cnt


class PipelineNode:
    def __init__(self, id):
        self.id = id
        self.label = None
        self.branch = None
        self.header = None
        self.message = None
        self.parent = None
        self.content = {}

    def get_branch(self):
        if self.branch:
            return self.branch
        if self.parent:
            return self.parent.get_branch()
        return None


class BuildInfo:
    def __init__(
        self,
        fetcher,
        job_name,
        build_number,
        stage=None,
        fetch_on_init=True,
        cache=None,
        fetch_sections=True,
        upstream=None,
        virtual=False,
    ):
        self.fetcher = fetcher
        self.virtual = virtual
        self.job_name = job_name

        self._build_number = None
        self.build_number_str = build_number
        try:
            self._build_number = int(build_number)
        except ValueError:
            pass

        self._build_url = None

        self.stage = stage

        self._info_fetched = False
        self._job_type = None
        self._queueing_duration = None
        self._start = None
        self._duration = None
        self._result = None
        self._node_name = None

        self.user = None

        self.upstream = upstream
        self._sub_builds = None
        self.__all_builds = None

        self._fetch_sections = fetch_sections
        self._sections = None

        self._parameters = None

        self.build_json = None
        self._raw_data = None

        self._description = None
        self._failure_causes = None
        self._console_log = None

        self.cache = cache

        self.lane_index = None

        if fetch_on_init:
            self.fetch()

    def fetch(self, fatal=False):
        self._fetch_info(fatal)

        if self._sub_builds is None:
            self._fetch_sub_builds()

        if self._fetch_sections == "done":
            # Only fetch sections if the top build is done
            self._fetch_sections = self.is_done
        if self._sections is None and self._fetch_sections is True:
            self.__determine_sections()

    def build_url(self, extra=""):
        if self._build_url:
            return urljoin(self._build_url, extra)
        return urljoin(
            self.fetcher.url,
            "/".join(["job", self.job_name, self.build_number_str, extra]),
        )

    def set_build_url(self, value):
        self._build_url = value

    def __fetch_build_data(self, extra="", encoding="ISO-8859-1"):
        raw_data = None
        cache_key = None
        api_url = self.build_url(extra)
        if self.cache and self._build_number:
            cache_key = "jenkins-build-analyzer-%s" % api_url
            raw_data = self.cache.get(cache_key)

        if raw_data:
            logger.info("Content for '%s' already cached", api_url)
            return (raw_data, True, cache_key)

        logger.info("Fetching info from '%s'", api_url)

        content = pool_manager.urlopen("GET", api_url)
        if content.status != 200:
            raise BuildNotFoundException(self)

        raw_data = content.data.decode(encoding)

        return (raw_data, False, cache_key)

    def get_build_json(self):
        if self.build_json:
            return self.build_json

        self._raw_data, content_cached, cache_key = self.__fetch_build_data(
            "api/json?depth=3"
        )
        try:
            self.build_json = json.loads(self._raw_data)
        except json.decoder.JSONDecodeError as ex:
            logger.error("Unable to parse JSON at '%s'", self.build_url())
            logger.error(ex)
            raise BuildNotFoundException(self)

        if self.cache and cache_key and not content_cached and self.is_done:
            try:
                # Cache the content for 5h
                self.cache.set(cache_key, self._raw_data, (5 * 60 * 60))
            except Exception:
                logger.exception("Unable to set cache for build xml")

        return self.build_json

    # Retrieve the XML from Jenkins that contains some info about
    # the build.
    def _fetch_info(self, fatal=False):

        if self._info_fetched:
            logger.warning("Skipping fetch info as already fetched")
            return
        self._info_fetched = True

        # Provide reasonable defaults in case the build is not found
        self._failure_causes = []
        self._start = 0
        if self.upstream:
            self._start = self.upstream.start
        self._duration = 0
        self._queueing_duration = 0
        self._parameters = {}

        if self.virtual:
            return

        logger.debug("Fetching object %s", self)

        tree = self.get_build_json()

        job_type = tree["_class"]
        if job_type == "org.jenkinsci.plugins.workflow.job.WorkflowRun":
            self._job_type = "pipeline"
        elif job_type == "hudson.model.FreeStyleBuild":
            self._job_type = "freestyle"
        else:
            self._job_type = job_type

        self._start = int(tree["timestamp"])
        self._duration = int(tree["duration"])

        self._description = tree.get("description")

        for action in tree.get("actions", []):
            action_class = action.get("_class")
            if not action_class:
                continue
            if action_class == "jenkins.metrics.impl.TimeInQueueAction":
                self._queueing_duration = int(action["queuingDurationMillis"])
            elif action_class == "hudson.model.Cause":
                for cause_elmt in action["causes"]:
                    cause_class = cause_elmt.get("_class")
                    if cause_class == "hudson.model.Cause$UpstreamCause":
                        upstream_job = cause_elmt["upstreamProject"]
                        upstream_build = cause_elmt["upstreamBuild"]
                        if upstream_job and upstream_build:
                            self.upstream = self.fetcher.get_build(
                                upstream_job,
                                upstream_build,
                                fetch=False,
                                fetch_sections=False,
                            )
                    elif cause_class == "hudson.model.Cause$UserIdCause":
                        user_id = cause_elmt["userId"]
                        user_name = cause_elmt["userName"]
                        self.user = {"user_id": user_id, "user_name": user_name}
            elif (
                action_class
                == "com.sonyericsson.jenkins.plugins.bfa.model.FailureCauseBuildAction"
            ):
                for cause_elmt in action["foundFailureCauses"]:
                    cause = {}
                    name = cause_elmt.get("name")
                    if name is None:
                        continue

                    cause["name"] = name
                    desc = cause_elmt.get("description")
                    if desc is not None:
                        cause["description"] = desc.strip()
                    cause["categories"] = []
                    categories = cause_elmt.get("categories")
                    if categories:
                        for cat in categories:
                            cause["categories"].append(cat)
                    self._failure_causes.append(cause)
            elif action_class == "hudson.model.ParametersAction":
                for param_elmt in action["parameters"]:
                    name_elmt = param_elmt.get("name")
                    value_elmt = param_elmt.get("value")
                    if isinstance(value_elmt, bool):
                        value_elmt = str(value_elmt).lower()
                    else:
                        value_elmt = str(value_elmt)
                    if name_elmt is None:
                        logger.warning(
                            "Missing name element for parameter %s", param_elmt
                        )
                        continue
                    if value_elmt is None:
                        logger.warning(
                            "Missing value element for parameter %s", name_elmt
                        )
                        continue
                    param = {
                        "class_name": param_elmt["_class"],
                        "name": name_elmt,
                        "value": value_elmt,
                    }
                    self._parameters[param["name"]] = param

        logger.debug(
            "%s#%s: %s %d %d %d %s"
            % (
                self.job_name,
                self.build_number,
                self._job_type,
                self._start,
                self._queueing_duration,
                self._duration,
                self._result,
            )
        )

    @property
    def job_type(self):
        if not self._job_type:
            self._fetch_info()

        return self._job_type

    @job_type.setter
    def job_type(self, value):
        self._job_type = value

    @property
    def start(self):
        if not self._start:
            self._fetch_info()

        return self._start

    @start.setter
    def start(self, value):
        self._start = value

    @property
    def end(self):
        if self.start and self.duration:
            return self.start + self.duration
        return None

    @end.setter
    def end(self, value):
        if self._start:
            self._duration = value - self.start

    @property
    def queueing_duration(self):
        if self._queueing_duration is None:
            self._fetch_info()

        return self._queueing_duration

    @queueing_duration.setter
    def queueing_duration(self, value):
        self._queueing_duration = value

    @property
    def duration(self):
        if self._duration is None:
            self._fetch_info()

        if self.result == "IN_PROGRESS":
            now_ts = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            assert now_ts >= self.start
            duration = now_ts - self.start
            return duration

        return self._duration

    @duration.setter
    def duration(self, value):
        self.duration = value

    @property
    def parameters(self):
        if self._parameters is None:
            self._fetch_info()

        return self._parameters

    @property
    def node_name(self):
        if self._node_name:
            return self._node_name

        if not self.build_json:
            self._fetch_info()

        if not self.build_json:
            return

        self._node_name = self.build_json.get("builtOn")

        return self._node_name

    @property
    def build_number(self):
        if self._build_number is not None:
            return self._build_number

        if not self.build_json:
            self._fetch_info()

        self._build_number = int(self.build_json["number"])
        return self._build_number

    @property
    def is_done(self):
        return (self.result != "IN_PROGRESS") and (self.result != "UNKNOWN")

    def __check_infra_failure(self):
        for cause in self.failure_causes:
            if "retrigger" in cause["categories"]:
                return "INFRA_FAILURE"
        return "FAILURE"

    @property
    def result(self):
        if self._result:
            return self._result

        if not self.build_json:
            self._fetch_info()

        # Determine result
        result = None
        if not self.build_json:
            result = "UNKNOWN"
        elif self.build_json.get("building"):
            result = "IN_PROGRESS"
        elif self.build_json.get("result") is not None:
            result = self.build_json.get("result")
        else:
            result = "UNKNOWN"

        if result == "FAILURE":
            result = self.__check_infra_failure()

        self._result = result

        return self._result

    @result.setter
    def result(self, value):
        if value == "FAILURE":
            value = self.__check_infra_failure()
        self._result = value

    @property
    def description(self):
        if self._description is None:
            self._fetch_info()

        return self._description

    @description.setter
    def description(self, value):
        self._description = value

    @property
    def failure_causes(self):
        if not self._failure_causes:
            self._fetch_info()

        return self._failure_causes

    @property
    def console_log(self):
        if self._console_log:
            return self._console_log

        url_extra = "consoleText"
        if self.job_type == "pipeline":
            url_extra = "logText/progressiveHtml"

        raw_data, content_cached, cache_key = self.__fetch_build_data(url_extra)

        self._console_log = raw_data

        if (
            self.cache
            and cache_key
            and not content_cached
            and self._result != "IN_PROGRESS"
        ):
            try:
                # Cache the content for 5h
                self.cache.set(cache_key, self._console_log, (5 * 60 * 60))
            except Exception:
                logger.exception(
                    "Unable to set cache for console log (size=%s)",
                    len(self._console_log),
                )

        return self._console_log

    def create_sub_build(self, job_name, build_number, stage=""):
        sub_build = self.fetcher.get_build(job_name, build_number, fetch=False)
        sub_build.stage = stage
        sub_build.upstream = self
        try:
            sub_build.fetch()
        except BuildNotFoundException as ex:
            logger.warning(ex)

        # Append
        if not self._sub_builds:
            self._sub_builds = []
        self._sub_builds.append(sub_build)

        return sub_build

    def __parse_pipeline_log(self):

        try:
            doc = BeautifulSoup(
                "<html>{0}</html>".format(self.console_log), features="html.parser"
            )
        except ET.ParseError as e:
            logger.error("Unable to parse HTML from '%s'", self.build_url())
            logger.error(e)
            return

        pattern = re.compile(r"/job/(?P<job>.+)/(?P<bn>\d+)/")

        nodes = {}

        for span in doc.find_all("span"):
            if "class" not in span.attrs:
                continue

            span_class = span.attrs["class"][0]

            if span_class == "pipeline-new-node":
                node_id = span.attrs["nodeid"]

                # start_id = span.attrs['startid']

                node = PipelineNode(node_id)
                node.header = span.text
                if "enclosingid" in span.attrs:
                    enclosing_id = span.attrs["enclosingid"]
                    if not nodes[enclosing_id]:
                        logger.error("Node %s does not exist", enclosing_id)
                        continue

                    nodes[enclosing_id].content[node_id] = node
                    node.parent = nodes[enclosing_id]

                if "label" in span.attrs:
                    node.label = span.attrs["label"]
                    if node.label.startswith("Branch: "):
                        node.branch = span.attrs["label"].replace("Branch: ", "")

                nodes[node_id] = node

            elif span_class.startswith("pipeline-node-"):
                node_id = span_class.replace("pipeline-node-", "")
                if node_id not in nodes:
                    logger.warning("Node %s not found", node_id)
                    continue

                node = nodes[node_id]

                branch = None
                if node.parent:
                    branch = node.get_branch()

                if "Starting building:" not in span.text:
                    continue

                match = None
                for job_link in span.find_all("a"):

                    job_href = job_link.attrs["href"]

                    match = pattern.match(job_href)
                    if match is None:
                        continue

                    job_name = match.group("job")
                    build_number = match.group("bn")
                    if job_name and build_number:
                        branch_info = ""
                        if branch:
                            branch_info = "[%s]" % branch
                        logger.debug(
                            "Sub-build: %s#%s %s", job_name, build_number, branch_info
                        )

                        try:
                            self.create_sub_build(job_name, build_number, branch)

                        except BuildNotFoundException as ex:
                            logger.error(ex)
                            logger.warning(branch)

                if match is None:
                    logger.warning("No link found for %s", span.text)

            else:
                logger.debug(span)

    # Retrieve the 'sub-builds', which are launched from this job.
    def _fetch_sub_builds(self):
        self._sub_builds = []

        if self.virtual:
            return

        if self.job_type not in ["pipeline"]:
            return

        # Parse log as HTML
        if self.job_type == "pipeline":
            self.__parse_pipeline_log()

        logger.info(
            "%s#%s (%s): %d sub-build(s)",
            self.job_name,
            self.build_number,
            self.job_type,
            len(self._sub_builds),
        )

    def __determine_sections(self):
        self._sections = []

        if self.job_type != "freestyle":
            return

        pattern = re.compile(
            r"^(?:.\[95m)?\[section:(?P<name>[^\]]*)\] (?P<boundary>start|end)? *"
            "(time=(?P<time>[0-9]*))? *"
            "(type=(?P<type>[a-z]*))? *"
            "(.*)"
        )
        pattern_reset = re.compile(".*Executing post build scripts.*")
        current = None

        for line in self.console_log.splitlines():
            if pattern_reset.match(line):
                # If the build was aborted while another section was in progress,
                # stop processing the current section.
                current = None
                continue

            m = pattern.match(line)
            if not m:
                if (
                    "[section:" in line
                    and "message" not in line
                    and "echo -e" not in line
                ):
                    logger.warning("'%s' not matched", line)
                continue

            logger.debug("Section: %s", line)

            boundary = m.group("boundary")
            name = m.group("name")
            section_type = m.group("type")
            time = 0
            if m.group("time"):
                time = int(m.group("time")) * 1000
            else:
                logger.warning("No time in section '%s'", line)

            if boundary == "start":
                # Start
                new = BuildSection(name, section_type)
                self._sections.append(new)

                if current:
                    new.parent = current
                    current.children.append(new)

                current = new
                current.start = time
            elif boundary == "end":
                # End
                if current:
                    current.end = time
                    current = current.parent
                else:
                    logger.warning(
                        "Noticed a end section while no section is in progress"
                    )
            else:
                raise Exception("Unknown boundary %s" % boundary)

        for section in self._sections:
            logger.debug(
                "Section: %s %s %s", section.name, section.type, section.duration
            )

    @property
    def sub_builds(self):
        if self._sub_builds is None:
            try:
                self._fetch_sub_builds()
            except BuildNotFoundException:
                pass

        return self._sub_builds

    @property
    def all_builds(self):
        if self.__all_builds is None:
            blds = [self]

            for bld in self.sub_builds:
                blds += bld.all_builds

            self.__all_builds = blds

        return self.__all_builds

    @property
    def sections(self):
        return self._sections


class BuildInfoFetcher:
    def __init__(self, url, cache=None, info_class=BuildInfo, fetch_sections=True):
        self.url = url
        self.cache = cache
        self.info_class = info_class
        self.fetch_sections = fetch_sections
        self.builds = {}

    def _create_build(self, job_name, build_number, fetch_sections=None):
        if fetch_sections is None:
            fetch_sections = self.fetch_sections
        return self.info_class(
            self,
            job_name,
            build_number,
            fetch_on_init=False,
            cache=self.cache,
            fetch_sections=fetch_sections,
        )

    def get_build(
        self, job_name, build_number, fetch=True, fetch_sections=None, fatal=False
    ):
        build_id = "%s #%s" % (job_name, build_number)
        if build_id not in self.builds:
            build = self._create_build(job_name, build_number)
            if fetch:
                build.fetch(fatal=fatal)
            self.builds[build_id] = build

        return self.builds[build_id]

    def fetch(self, job_name, build_number, fatal=False):
        return self.get_build(job_name, build_number, fatal=fatal)
