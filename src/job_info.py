import urllib3
import xml.etree.ElementTree as ET
import re
import logging
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

pool_manager = urllib3.PoolManager(timeout=30.0)

class JobInfoFetcher:

    def fetch(url, job_name, build_number):
        job_info = JobInfo(url, job_name, build_number)
        return job_info

class JobNotFoundException(Exception):

    def __init__(self, job_info):

        super(JobNotFoundException, self).__init__("Job %s#%s not found" % (job_info.job_name, job_info.build_number))

class BuildSection:

    def __init__(self, name, section_type=None):
        self.name = name
        self.__type = section_type
        self.parent = None
        self.start = None
        self.end = None

    def type(self):
        if self.__type:
            return self.__type

        if self.parent:
            return self.parent.type()

        return None

    def duration(self):
        if not self.start or not self.end:
            return 0

        return (self.end - self.start)  # in us

    def parents_cnt(self):
        cnt = 0
        if self.parent != None:
            cnt = 1 + self.parent.parents_cnt()
        return cnt

class JobInfo:

    def __init__(self, url, job_name, build_number, stage=None, fetch_on_init=True):
        self.url = url
        self.job_name = job_name
        self.build_number = build_number
        self.stage = stage

        self.__job_type = None
        self.__queueing_duration = None
        self.__start = None
        self.__duration = None
        self.__result = None

        self.__sub_builds = None
        self.__all_builds = None
        self.__sections = None

        self.__console_log = None

        if fetch_on_init:
            self.fetch()

    def fetch(self):
        self.__fetch_info()
        self.__fetch_sub_builds()
        self.__determine_sections()

    def build_url(self, extra=""):
        return urljoin(self.url, '/'.join(['job', self.job_name, str(self.build_number), extra]))

    # Retrieve the XML from Jenkins that contains some info about
    # the build.
    def __fetch_info(self):
        url = self.build_url('api/xml?depth=3')
        logger.info("Fetching info from '%s'" % url)

        content = pool_manager.urlopen('GET', url)
        if content.status != 200:
            raise JobNotFoundException(self)

        raw_data = content.data.decode()

        try:
            tree = ET.XML(raw_data)
        except ET.ParseError:
            logger.error("Unable to parse XML at '%s'" % url)
            raise JobNotFoundException(self)

        job_type = tree.tag
        if job_type == "workflowRun":
            self.__job_type = 'pipeline'
        elif job_type == "flowRun":
            self.__job_type = 'buildFlow'
        elif job_type == "freeStyleBuild":
            self.__job_type = 'freestyle'
        else:
            self.__job_type = job_type

        self.__start = int(tree.find('./timestamp').text)
        self.__duration = int(tree.find('./duration').text)
        if tree.find('./result') != None:
            self.__result = tree.find('./result').text
        elif tree.find('./building') != None and tree.find('./building').text == 'true':
            self.__result = 'IN_PROGRESS'
        else:
            self.__result = 'UNKNOWN'

        self.__queueing_duration = 0
        if tree.find('./action/queuingDurationMillis') != None:
            self.__queueing_duration = int(tree.find('./action/queuingDurationMillis').text)

        self.__failure_causes = []
        for cause_elmt in tree.iterfind('./action/foundFailureCause'):
            cause = {}
            name = cause_elmt.find('name')
            if name == None:
                continue

            cause['name'] = name.text
            desc = cause_elmt.find('description')
            if desc != None:
                cause['description'] = desc.text.strip()
            cause['categories'] = []
            for cat in cause_elmt.iter('category'):
                cause['categories'].append(cat.text)
            self.__failure_causes.append(cause)

            if (self.__result == "FAILURE") and ('retrigger' in cause['categories']):
                self.__result = "INFRA_FAILURE"

        logger.debug("%s#%s: %s %d %d %d %s" % (self.job_name, self.build_number, self.__job_type,
                                                                                  self.__start,
                                                                                  self.__queueing_duration,
                                                                                  self.__duration,
                                                                                  self.__result))

    def job_type(self):
        if not self.__job_type:
            self.__fetch_info()

        return self.__job_type

    def start(self):
        if not self.__start:
            self.__fetch_info()

        return self.__start

    def queueing_duration(self):
        if not self.__queueing_duration:
            self.__fetch_info()

        return self.__queueing_duration

    def duration(self):
        if not self.__duration:
            self.__fetch_info()

        return self.__duration

    def result(self):
        if not self.__result:
            self.__fetch_info()

        return self.__result

    def failure_causes(self):
        if not self.__failure_causes:
            self.__fetch_info()

        return self.__failure_causes

    def console_log(self):
        if self.__console_log is None:
            base_url = '/'.join([self.url,
                                'job',
                                self.job_name,
                                self.build_number])

            url = '/'.join([base_url, 'consoleText'])
            if self.job_type() == 'pipeline':
                url = '/'.join([base_url, 'logText/progressiveHtml'])
            logger.info("Fetching log from %s" % url)

            content = pool_manager.urlopen('GET', url)
            self.__console_log = content.data.decode('latin-1')

        return self.__console_log

    def __parse_build_flow_log(self):
        pattern = re.compile("(?P<stage>) *Build (?P<job>.+) #(?P<bn>\d+) started")

        for line in self.console_log().splitlines():

            m = pattern.match(line)
            if not m:
                continue

            logger.debug("Line: %s" % line)

            job = m.group('job')
            build_number = m.group('bn')
            stage = m.group('stage')
            stage_info = ""
            if stage:
                stage_info = "[%s]" % stage
            logger.debug("Sub-build: %s#%s %s" % (job, build_number, stage_info))

            try:
                job = JobInfo(self.url, job, build_number, stage)
                self.__sub_builds.append(job)

            except JobNotFoundException as e:
                logger.error(e)

    def __parse_pipeline_log(self):
        doc = ET.fromstring('<html>{0}</html>'.format(self.console_log()))

        pattern = re.compile("/job/(?P<job>.+)/(?P<bn>\d+)/")

        enclosing_ids = {}
        node_enclosing_ids = {}
        for span in doc.iter('span'):
            #logger.debug(span.attrib)

            if 'class' not in span.attrib:
                continue

            if span.attrib['class'] == 'pipeline-new-node' and \
               'enclosingId' in span.attrib and \
               'startId' in span.attrib and \
               'label' in span.attrib:
                start_id = span.attrib['startId']
                if span.attrib['label'].startswith('Branch: '):
                    enclosing_ids[start_id] = span.attrib['label'].replace('Branch: ', '')

            elif span.attrib['class'] == 'pipeline-new-node' and \
               'enclosingId' in span.attrib and \
               'nodeId' in span.attrib:
                enclosing_id = span.attrib['enclosingId']
                node_id = span.attrib['nodeId']
                node_enclosing_ids[node_id] = enclosing_id

            elif span.attrib['class'].startswith('pipeline-node-'):
                node_id = span.attrib['class'].replace('pipeline-node-', '')
                if node_id not in node_enclosing_ids:
                    logger.warn("Node %s not found" % node_id)
                    continue

                enclosing_id = node_enclosing_ids[node_id]
                branch = None
                if enclosing_id in enclosing_ids:
                    branch = enclosing_ids[enclosing_id]

                if 'Starting building:' not in span.text:
                    continue

                job_link = span.find('./a')
                if job_link == None:
                    continue

                job_href = job_link.attrib['href']
                logger.debug(job_href)

                m = pattern.match(job_href)
                if not m:
                    continue

                job = m.group('job')
                build_number = m.group('bn')
                if job and build_number:
                    if branch:
                        branch_info = "[%s]" % branch
                    logger.debug("Sub-build: %s#%s %s" % (job, build_number, branch_info))

                    try:
                        job = JobInfo(self.url, job, build_number, branch)
                        self.__sub_builds.append(job)

                    except JobNotFoundException as e:
                        logger.error(e)
                        logger.warn(current_branch)

    # Retreive the 'sub-builds', which are launched from this job.
    def __fetch_sub_builds(self):
        self.__sub_builds = []

        if self.job_type() != 'pipeline' and \
           self.job_type() != 'buildFlow':
            return

        # Parse log as HTML
        if self.job_type() == 'pipeline':
            self.__parse_pipeline_log()

        # Parse log
        elif self.job_type() == 'buildFlow':
            self.__parse_build_flow_log()

        logger.info("%s#%s: %d sub-build(s)" % (self.job_name, self.build_number, len(self.__sub_builds)))

    def __determine_sections(self):
        self.__sections = []

        if self.job_type() != 'freestyle':
            return

        pattern = re.compile("^\[section:(?P<name>[^\]]*)\] (?P<boundary>start|end)? *"
                                                           "(time=(?P<time>[0-9]*))? *"
                                                           "(type=(?P<type>[a-z]*))? *"
                                                           "(.*)")
        current = None

        for line in self.console_log().splitlines():

            m = pattern.match(line)
            if not m:
                continue

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

                current = new
                current.start = time
            elif boundary == "end":
                # End
                current.end = time
                current = current.parent
            else:
                raise Exception("Unknown boundary %s" % boundary)

        for section in self.__sections:
            logger.debug("Section: %s %s %s" % (section.name,
                                                section.type(),
                                                section.duration()))

    def sub_builds(self):
        if self.__sub_builds == None:
            self.__fetch_sub_builds()

        return self.__sub_builds

    def all_builds(self):
        if self.__all_builds == None:
            blds = [self]

            for bld in self.sub_builds():
                blds += bld.all_builds()

            self.__all_builds = blds

        return self.__all_builds

    def sections(self):
        return self.__sections

