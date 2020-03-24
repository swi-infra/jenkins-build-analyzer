import urllib3
import xml.etree.ElementTree as ET
import re
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

pool_manager = urllib3.PoolManager(timeout=30.0)


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
        dur_s = self.duration / 1000
        if dur_s < 60:
            info += " %ds" % (dur_s)
        else:
            info += " %dmin" % (dur_s / 60)
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
    ):
        self.fetcher = fetcher
        self.job_name = job_name

        self._build_number = None
        self.build_number_str = build_number
        try:
            self._build_number = int(build_number)
        except ValueError:
            pass

        self.stage = stage

        self.__job_type = None
        self.__queueing_duration = None
        self.__start = None
        self.__duration = None
        self.__result = None
        self.__node_name = None

        self.user = None

        self.upstream = upstream
        self.__sub_builds = None
        self.__all_builds = None

        self._fetch_sections = fetch_sections
        self.__sections = None

        self.__parameters = None

        self.build_xml = None
        self._raw_data = None

        self.__description = None
        self.__failure_causes = None
        self._console_log = None

        self.cache = cache

        if fetch_on_init:
            self.fetch()

    def fetch(self):
        self.__fetch_info()
        if self._fetch_sections == "done":
            # Only fetch sections if the top build is done
            self._fetch_sections = self.is_done
        self.__fetch_sub_builds()
        if self._fetch_sections is True:
            self.__determine_sections()

    def build_url(self, extra=""):
        return urljoin(
            self.fetcher.url,
            "/".join(["job", self.job_name, self.build_number_str, extra]),
        )

    def __fetch_build_data(self, extra="", encoding="ISO-8859-1"):
        raw_data = None
        cache_key = None
        api_url = self.build_url(extra)
        if self.cache and self._build_number:
            cache_key = "jenkins-build-analyzer-%s" % api_url
            raw_data = self.cache.get(cache_key)

        if raw_data:
            logger.info("Content for '%s' already cached" % api_url)
            return (raw_data, True, cache_key)

        logger.info("Fetching info from '%s'" % api_url)

        content = pool_manager.urlopen("GET", api_url)
        if content.status != 200:
            raise BuildNotFoundException(self)

        raw_data = content.data.decode(encoding)

        return (raw_data, False, cache_key)

    def get_build_xml(self):
        if self.build_xml:
            return self.build_xml

        self._raw_data, content_cached, cache_key = self.__fetch_build_data(
            "api/xml?depth=3"
        )
        try:
            self.build_xml = ET.XML(self._raw_data)
        except ET.ParseError:
            logger.error("Unable to parse XML at '%s'" % self.build_url())
            raise BuildNotFoundException(self)

        if self.cache and cache_key and not content_cached and self.is_done:
            # Cache the content for 5h
            self.cache.set(cache_key, self._raw_data, (5 * 60 * 60))

        return self.build_xml

    # Retrieve the XML from Jenkins that contains some info about
    # the build.
    def __fetch_info(self):
        tree = self.get_build_xml()

        job_type = tree.tag
        if job_type == "workflowRun":
            self.__job_type = "pipeline"
        elif job_type == "flowRun":
            self.__job_type = "buildFlow"
        elif job_type == "freeStyleBuild":
            self.__job_type = "freestyle"
        else:
            self.__job_type = job_type

        self.__start = int(tree.find("./timestamp").text)
        self.__duration = int(tree.find("./duration").text)

        self.__queueing_duration = 0
        if tree.find("./action/queuingDurationMillis") is not None:
            self.__queueing_duration = int(
                tree.find("./action/queuingDurationMillis").text
            )

        if tree.find("./description") is not None:
            self.__description = tree.find("./description").text

        for cause_elmt in tree.iterfind("./action/cause"):
            cause_class = cause_elmt.get("_class")
            if cause_class == "hudson.model.Cause$UpstreamCause":
                upstream_job = cause_elmt.findtext("upstreamProject")
                upstream_build = cause_elmt.findtext("upstreamBuild")
                if upstream_job and upstream_build:
                    self.upstream = self.fetcher.get_build(
                        upstream_job, upstream_build, fetch=False, fetch_sections=False
                    )
            elif cause_class == "hudson.model.Cause$UserIdCause":
                user_id = cause_elmt.findtext("userId")
                user_name = cause_elmt.findtext("userName")
                self.user = {"user_id": user_id, "user_name": user_name}

        self.__failure_causes = []
        for cause_elmt in tree.iterfind("./action/foundFailureCause"):
            cause = {}
            name = cause_elmt.find("name")
            if name is None:
                continue

            cause["name"] = name.text
            desc = cause_elmt.find("description")
            if desc is not None:
                cause["description"] = desc.text.strip()
            cause["categories"] = []
            for cat in cause_elmt.iter("category"):
                cause["categories"].append(cat.text)
            self.__failure_causes.append(cause)

        self.__parameters = {}
        for param_elmt in tree.iterfind("./action/parameter"):
            name_elmt = param_elmt.find("name")
            value_elmt = param_elmt.find("value")
            if name_elmt is None:
                logger.warning("Missing name element for parameter %s" % param_elmt)
                continue
            if value_elmt is None:
                logger.warning(
                    "Missing value element for parameter %s" % name_elmt.text
                )
                continue
            param = {
                "class_name": param_elmt.attrib["_class"],
                "name": name_elmt.text,
                "value": value_elmt.text,
            }
            self.__parameters[param["name"]] = param

        logger.debug(
            "%s#%s: %s %d %d %d %s"
            % (
                self.job_name,
                self.build_number,
                self.__job_type,
                self.__start,
                self.__queueing_duration,
                self.__duration,
                self.__result,
            )
        )

    @property
    def job_type(self):
        if not self.__job_type:
            self.__fetch_info()

        return self.__job_type

    @property
    def start(self):
        if not self.__start:
            self.__fetch_info()

        return self.__start

    @property
    def queueing_duration(self):
        if not self.__queueing_duration:
            self.__fetch_info()

        return self.__queueing_duration

    @property
    def duration(self):
        if not self.__duration:
            self.__fetch_info()

        return self.__duration

    @property
    def parameters(self):
        if self.__parameters is None:
            self.__fetch_info()

        return self.__parameters

    @property
    def node_name(self):
        if self.__node_name:
            return self.__node_name

        if not self.build_xml:
            self.__fetch_info()

        built_on = self.build_xml.find("./builtOn")
        if built_on is not None:
            self.__node_name = built_on.text

        return self.__node_name

    @property
    def build_number(self):
        if self._build_number is not None:
            return self._build_number

        if not self.build_xml:
            self.__fetch_info()

        self._build_number = int(self.build_xml.find("./number").text)
        return self._build_number

    @property
    def is_done(self):
        return (self.result != "IN_PROGRESS") and (self.result != "UNKNOWN")

    @property
    def result(self):
        if self.__result:
            return self.__result

        if not self.build_xml:
            self.__fetch_info()

        # Determine result
        result = None
        if (
            self.build_xml.find("./building") is not None
            and self.build_xml.find("./building").text == "true"
        ):
            result = "IN_PROGRESS"
        elif self.build_xml.find("./result") is not None:
            result = self.build_xml.find("./result").text
        else:
            result = "UNKNOWN"

        if result == "FAILURE":
            for cause in self.failure_causes:
                if "retrigger" in cause["categories"]:
                    result = "INFRA_FAILURE"
                    break

        self.__result = result

        return self.__result

    @property
    def description(self):
        if self.__description is None:
            self.__fetch_info()

        return self.__description

    @property
    def failure_causes(self):
        if not self.__failure_causes:
            self.__fetch_info()

        return self.__failure_causes

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
            and self.__result != "IN_PROGRESS"
        ):
            # Cache the content for 5h
            self.cache.set(cache_key, self._console_log, (5 * 60 * 60))

        return self._console_log

    def create_sub_build(self, job_name, build_number, stage):
        sub_build = self.fetcher.get_build(job_name, build_number)
        sub_build.stage = stage
        sub_build.upstream = self
        return sub_build

    def __parse_build_flow_log(self):
        pattern = re.compile(r"(?P<stage>) *Build (?P<job>.+) #(?P<bn>\d+) started")

        for line in self.console_log.splitlines():

            m = pattern.match(line)
            if not m:
                continue

            logger.debug("Line: %s" % line)

            job_name = m.group("job")
            build_number = m.group("bn")
            stage = m.group("stage")
            stage_info = ""
            if stage:
                stage_info = "[%s]" % stage
            logger.debug("Sub-build: %s#%s %s" % (job_name, build_number, stage_info))

            try:
                sub_build = self.create_sub_build(job_name, build_number, stage)
                self.__sub_builds.append(sub_build)

            except BuildNotFoundException as e:
                logger.error(e)

    def __parse_pipeline_log(self):

        try:
            doc = BeautifulSoup(
                "<html>{0}</html>".format(self.console_log), features="html.parser"
            )
        except ET.ParseError as e:
            logger.error("Unable to parse HTML from '%s'" % self.build_url())
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
                        logger.error("Node %s does not exist" % enclosing_id)
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
                    logger.warning("Node %s not found" % node_id)
                    continue

                node = nodes[node_id]

                branch = None
                if node.parent:
                    branch = node.get_branch()

                if "Starting building:" not in span.text:
                    continue

                m = None
                for job_link in span.find_all("a"):

                    job_href = job_link.attrs["href"]
                    logger.debug(job_href)

                    m = pattern.match(job_href)
                    if m:
                        break

                if m is None:
                    logger.warning("No link found for %s" % span.text)
                    continue

                job_name = m.group("job")
                build_number = m.group("bn")
                if job_name and build_number:
                    branch_info = ""
                    if branch:
                        branch_info = "[%s]" % branch
                    logger.debug(
                        "Sub-build: %s#%s %s" % (job_name, build_number, branch_info)
                    )

                    try:
                        sub_build = self.create_sub_build(
                            job_name, build_number, branch
                        )
                        self.__sub_builds.append(sub_build)

                    except BuildNotFoundException as e:
                        logger.error(e)
                        logger.warning(branch)

            else:
                logger.debug(span)

    # Retrieve the 'sub-builds', which are launched from this job.
    def __fetch_sub_builds(self):
        self.__sub_builds = []

        if self.job_type != "pipeline" and self.job_type != "buildFlow":
            return

        # Parse log as HTML
        if self.job_type == "pipeline":
            self.__parse_pipeline_log()

        # Parse log
        elif self.job_type == "buildFlow":
            self.__parse_build_flow_log()

        logger.info(
            "%s#%s: %d sub-build(s)"
            % (self.job_name, self.build_number, len(self.__sub_builds))
        )

    def __determine_sections(self):
        self.__sections = []

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
                if "[section:" in line:
                    logger.warn("'%s' not matched", line)
                continue

            logger.debug("Section: %s" % line)

            boundary = m.group("boundary")
            name = m.group("name")
            section_type = m.group("type")
            time = int(m.group("time")) * 1000
            if boundary == "start":
                # Start
                new = BuildSection(name, section_type)
                self.__sections.append(new)

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

        for section in self.__sections:
            logger.debug(
                "Section: %s %s %s" % (section.name, section.type, section.duration)
            )

    @property
    def sub_builds(self):
        if self.__sub_builds is None:
            self.__fetch_sub_builds()

        return self.__sub_builds

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
        return self.__sections


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

    def get_build(self, job_name, build_number, fetch=True, fetch_sections=None):
        build_id = "%s #%s" % (job_name, build_number)
        if build_id not in self.builds:
            self.builds[build_id] = self._create_build(job_name, build_number)
            if fetch:
                self.builds[build_id].fetch()

        return self.builds[build_id]

    def fetch(self, job_name, build_number):
        return self.get_build(job_name, build_number)
