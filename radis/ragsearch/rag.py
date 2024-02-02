import os
from openai import OpenAI

client = OpenAI(base_url=os.environ["BACKEND_URL"], api_key=os.environ["OPENAI_API_KEY"])

SYSTEM_PROMT = """Your are a helpful assistant with medical knowledge in the field of raiology. 
Your task is to help users to sort out relevant entries in a list of radiological reports, given 
a specific question about the contents of each of the reports. In the following, the list of reports 
is presented to you between the markers [BEGINLIST] and [ENDLIST]. Each report is enclosed between 
the markers [BEGINREPORT] and [ENDREPORT]. Each report has an ID, which is in the first line of the report, 
followed by a newline and the report text. The question is given between the markers [BEGINQUESTION] and [ENDQUESTION].
Do not answer verbally. Only list the ID of the reports that are relevant to the question in python integer array notation without any additional text.
Here is the list:
"""


class Rag(object):
    def __init__(self, query="", request="", reports=None):
        self.query = query
        self.request = request
        self.reports = reports
        self._ready = False
        self.run()

    def run(self):
        report_body_list = []
        for report in self.reports:
            report_full = report.report_full
            report_body_list.append(report_full.body)
            # print(report_full.id, "\n", report_full.body, "\n\n")
        n = 0
        c = 0
        reports_string = ""
        while n < len(report_body_list):
            if c < 5:
                reports_string += f"[BEGINREPORT] {n}\n{report_body_list[n]} [ENDREPORT]\n"
                n += 1
                c += 1
            else:
                prompt = (
                    f"<s>[INST] {SYSTEM_PROMT} \n[BEGINLIST]\n{reports_string}\n[ENDLIST]  [/INST]"
                )
                response = client.completions.create(
                    model=os.environ["LLM_MODEL"],
                    prompt=prompt,
                    max_tokens=2048,
                    stream=False,
                )
                print((response.choices[0].text))
                c = 0
                reports_string = ""
                reports_string += f"[BEGINREPORT] {n}\n{report_body_list[n]} [ENDREPORT]\n"
                n += 1
                c += 1
        self._ready = True

    def ready(self):
        return self._ready

    def get(self):
        return self.reports
