import urllib3
import os
import xml.etree.ElementTree as ET
import re
import coloredlogs, logging

logger = logging.getLogger(__name__)

pool_manager = urllib3.PoolManager(timeout=30.0)

class JobInfoFetcher:

    def fetch(url, job_name, build_number):
        job_info = JobInfo(url, job_name, build_number)
        return job_info

class JobNotFoundException(Exception):

    def __init__(self, job_info):

        super(JobNotFoundException, self).__init__("Job %s#%s not found" % (job_info.job_name, job_info.build_number))

class JobInfo:

    def __init__(self, url, job_name, build_number, fetch_on_init=True):
        self.url = url
        self.job_name = job_name
        self.build_number = build_number

        self.__job_type = None
        self.__timestamp = None
        self.__duration = None
        self.__result = None

        self.__sub_builds = None
        self.__all_builds = None

        if fetch_on_init:
            self.fetch()

    def fetch(self):
        self.__fetch_info()
        self.__fetch_sub_builds()

    # Retrieve the XML from Jenkins that contains some info about
    # the build.
    def __fetch_info(self):
        url = '/'.join([self.url, 'job', self.job_name, str(self.build_number), 'api/xml'])
        logger.info("Fetching info from %s" % url)

        headers = {'Connection': 'close'}
        content = pool_manager.urlopen('GET', url, headers=headers)
        if content.status != 200:
            raise JobNotFoundException(self)

        raw_data = content.data.decode()

        tree = ET.XML(raw_data)

        job_type = tree.tag
        if job_type == "workflowRun":
            self.__job_type = 'pipeline'
        elif job_type == "flowRun":
            self.__job_type = 'buildFlow'
        elif job_type == "freeStyleBuild":
            self.__job_type = 'freestyle'
        else:
            self.__job_type = job_type

        self.__timestamp = int(tree.find('./timestamp').text)
        self.__duration = int(tree.find('./duration').text)
        if tree.find('./result') != None:
            self.__result = tree.find('./result').text
        elif tree.find('./building') != None and tree.find('./building').text == 'true':
            self.__result = 'IN_PROGRESS'
        else:
            self.__result = 'UNKNOWN'

        logger.debug("%s#%s: %s %d %d %s" % (self.job_name, self.build_number, self.__job_type,
                                                                               self.__timestamp,
                                                                               self.__duration,
                                                                               self.__result))

    def job_type(self):
        if not self.__job_type:
            self.__fetch_info()

        return self.__job_type

    def timestamp(self):
        if not self.__timestamp:
            self.__fetch_info()

        return self.__timestamp

    def duration(self):
        if not self.__duration:
            self.__fetch_info()

        return self.__duration

    def result(self):
        if not self.__result:
            self.__fetch_info()

        return self.__result

    # Retreive the 'sub-builds', which are launched from this job.
    def __fetch_sub_builds(self):
        if self.job_type() != 'pipeline' and \
           self.job_type() != 'buildFlow':
            self.__sub_builds = []
            return

        url = '/'.join([self.url, 'job', self.job_name, self.build_number, 'consoleText'])
        logger.info("Fetching log from %s" % url)

        headers = {'Connection': 'close'}
        content = pool_manager.urlopen('GET', url, headers=headers)
        raw_data = content.data.decode()

        self.__sub_builds = []

        pattern = None
        if self.job_type() == 'pipeline':
            pattern = re.compile("(?:\[.*\] )?Starting building: (.+) #(\d+)")
        elif self.job_type() == 'buildFlow':
            pattern = re.compile(" *Build (.+) #(\d+) started")

        for line in raw_data.splitlines():

            m = pattern.match(line)
            if not m:
                continue

            logger.debug("Line: %s" % line)

            job = m.group(1)
            build_number = m.group(2)
            logger.debug("Sub-build: %s#%s" % (job, build_number))

            try:
                job = JobInfo(self.url, job, build_number)
                self.__sub_builds.append(job)

            except JobNotFoundException as e:
                logger.error(e)

        logger.info("%s#%s: %d sub-build(s)" % (self.job_name, self.build_number, len(self.__sub_builds)))

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
