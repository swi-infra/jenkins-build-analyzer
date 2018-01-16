import urllib3
import os
import xml.etree.ElementTree as ET
import re
import coloredlogs, logging

logger = logging.getLogger(__name__)

class JobInfoFetcher:

    def fetch(url, job_name, build_number):
        job_info = JobInfo(url, job_name, build_number)
        return job_info

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
        content = urllib3.PoolManager(timeout=10.0).urlopen('GET', url, headers=headers)
        raw_data = content.data.decode()

        tree = ET.XML(raw_data)

        job_type = tree.tag
        if job_type == "workflowRun":
            self.__job_type = 'pipeline'
        elif job_type == "freeStyleBuild":
            self.__job_type = 'freestyle'
        else:
            self.__job_type = job_type

        self.__timestamp = int(tree.find('./timestamp').text)
        self.__duration = int(tree.find('./duration').text)
        self.__result = tree.find('./result').text

        logger.debug("%s#%d: %s %d %d %s" % (self.job_name, self.build_number, self.__job_type,
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
        if self.job_type() != 'pipeline':
            self.__sub_builds = []
            return

        url = '/'.join([self.url, 'job', self.job_name, str(self.build_number), 'consoleText'])
        logger.info("Fetching log from %s" % url)

        headers = {'Connection': 'close'}
        content = urllib3.PoolManager(timeout=30.0).urlopen('GET', url, headers=headers)
        raw_data = content.data.decode()

        self.__sub_builds = []

        pattern = re.compile("(?:\[(.*)\] )?Starting building: (.+) #(\d+)")
        for line in raw_data.splitlines():
            m = pattern.match(line)
            if not m:
                continue

            logger.debug("Line: %s" % line)

            job = m.group(2)
            build_number = int(m.group(3))
            logger.debug("Sub-build: %s %d" % (job, build_number))

            self.__sub_builds.append( JobInfo(self.url, job, build_number) )

        logger.info("%s#%d: %d sub_builds" % (self.job_name, self.build_number, len(self.__sub_builds)))

