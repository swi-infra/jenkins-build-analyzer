
import svgwrite
import coloredlogs, logging
import math

logger = logging.getLogger(__name__)

STYLES = """rect        { stroke-width: 1; stroke-opacity: 0; }
      rect.background   { fill: rgb(255,255,255); }
      rect.aborted      { fill: rgb(190,200,183); fill-opacity: 0.7; }
      rect.success      { fill: rgb(170,255,170); fill-opacity: 0.7; }
      rect.failure      { fill: rgb(255,170,170); fill-opacity: 0.7; }
      rect.unstable     { fill: rgb(255,204,170); fill-opacity: 0.7; }
      rect.other        { fill: rgb(204,204,204); fill-opacity: 0.7; }
      rect.in_progress  { fill: rgb(135,205,222); fill-opacity: 0.7; }
      rect.box          { fill: rgb(240,240,240); stroke: rgb(192,192,192); }
      line       { stroke: rgb(64,64,64); stroke-width: 1; }
//    line.min1  { }
      line.min5  { stroke-width: 2; }
      line.min01 { stroke: rgb(224,224,224); stroke-width: 1; }
      text       { font-family: Verdana, Helvetica; font-size: 14px; }
      text.left  { font-family: Verdana, Helvetica; font-size: 14px; text-anchor: start; }
      text.right { font-family: Verdana, Helvetica; font-size: 14px; text-anchor: end; }
      text.min   { font-size: 10px; }
"""

class SvgPrinter:

    def __init__(self, job_info):
        self.job_info = job_info

        self.margin = 20

        self.build_padding = 5
        self.build_height = 30
        self.minute_width = 10

        self.__dwg = None

    def __determine_sizes(self):

        self.base_timestamp = self.job_info.timestamp()

        # Height based on number of builds to show
        self.box_height = self.build_height*len(self.job_info.all_builds())
        self.total_height = 2*self.margin + self.box_height

        # Width based on duration
        self.max_duration = self.job_info.duration()
        if self.job_info.result() == "IN_PROGRESS":
            for build in self.job_info.all_builds():
                self.max_duration = max(self.max_duration, build.timestamp() + build.duration() - self.base_timestamp)

        self.max_duration = math.ceil(self.max_duration / 1000 / 60 / 5) * 5 # in minutes, rounded up

        self.box_width = self.minute_width*self.max_duration
        self.total_width = 2*self.margin + self.box_width


        logger.debug("Total: %d x %d" % (self.total_height, self.total_width))

    def __render_grid(self):
        dwg = self.__dwg

        self.current_pos = self.margin

        dwg.add(dwg.rect(insert=(self.margin, self.margin),
                         size=(self.box_width, self.box_height),
                         class_='box'))

        for x in range(0, self.max_duration):

            class_name = "min01"
            if x % 5 == 0:
                class_name = "min5"

                dwg.add(dwg.text("%dmin" % x,
                                 insert=(self.current_pos, self.margin-5),
                                 class_="min"))

            dwg.add(dwg.line(start=(self.current_pos, self.margin),
                             end=(self.current_pos, self.total_height-self.margin),
                             class_=class_name))

            self.current_pos += self.minute_width

    def __render_build(self, build, index):
        dwg = self.__dwg

        offset = (build.timestamp() - self.base_timestamp)  / 1000 / 60
        offset_px = offset * self.minute_width

        duration = build.duration() / 1000 / 60
        if build.result() == "IN_PROGRESS":
            duration = self.max_duration - offset
        duration_px = duration * self.minute_width

        class_name = 'other'
        if build.result() == "SUCCESS":
            class_name = 'success'
        elif build.result() == "ABORTED":
            class_name = 'aborted'
        elif build.result() == "FAILURE":
            class_name = 'failure'
        elif build.result() == "UNSTABLE":
            class_name = 'unstable'
        elif build.result() == "IN_PROGRESS":
            class_name = 'in_progress'

        x = self.margin + offset_px
        y = self.margin + index*self.build_height


        dwg.add(dwg.rect(insert=(x, y + self.build_padding),
                         size=(duration_px, self.build_height - 2*self.build_padding),
                         class_=class_name))

        dwg.add(dwg.text("%s#%s" % (build.job_name, build.build_number),
                         insert=(x + 5, y + self.build_height - self.build_padding - 6),
                         class_="min"))



    def __render_builds(self):
        current_idx = 0
        for build in self.job_info.all_builds():
            self.__render_build(build, current_idx)
            current_idx += 1

    def print(self, output):
        print("Output to %s" % output)

        self.__determine_sizes()

        self.__dwg = svgwrite.Drawing(filename=output,
                                      size=(self.total_width, self.total_height),
                                      debug=True)

        dwg = self.__dwg

        # Add styles
        dwg.defs.add(dwg.style(STYLES))

        # Background
        dwg.add(dwg.rect(insert=(0, 0),
                         size=('100%', '100%'),
                         class_='background'))

        # Render grid
        self.__render_grid()

        # Render builds
        self.__render_builds()

        # Save
        dwg.save(pretty=True)

