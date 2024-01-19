SYSTEM_PROMT = """Your are a helpful assistant with medical knowledge in the field of raiology. 
Your task is to help users to sort out relevant entries in a list of radiological reports, given 
a specific question about the contents of each of the reports. In the following, the list of reports 
is presented to you between the markers [BEGINLIST] and [ENDLIST]. Each report is enclosed between 
the markers [BEGINREPORT] and [ENDREPORT]. Each report has an ID, which is in the first line of the report, 
followed by a newline and the report text. The question is given between the markers [BEGINQUESTION] and [ENDQUESTION].
In your answer, please only list the ID of the reports that are relevant to the question in python integer array notation.
"""


class Rag(object):
    def __init__(self, query="", request="", reports=None):
        self.query = query
        self.request = request
        self.reports = reports
        self._ready = False
        self.run()

    def run(self):
        for report in self.reports:
            report_full = report.report_full
            # print(report_full.id, "\n", report_full.body, "\n\n")
        self._ready = True

    def ready(self):
        return self._ready

    def get(self):
        return self.reports
