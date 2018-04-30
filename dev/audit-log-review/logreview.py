#!/usr/bin/python

import sys
import os
import glob
import re
import json

from urlparse import urlparse
from collections import defaultdict
from pprint import pprint
# https://github.com/kubernetes/kubernetes/pull/50627/files


"https://raw.githubusercontent.com/kubernetes/kubernetes/v1.9.6/api/openapi-spec/swagger.json"

# inputs
# audit log
# swagger json defs
#

def defaultdicttree():
    return defaultdict(defaultdicttree)

def defaultdict_to_dict(d):
    if isinstance(d, defaultdict):
        new_d = {}
        for k, v in d.items():
            new_d[k] = defaultdict_to_dict(v)
        d = new_d 
    return d 


prefix_cache = defaultdict(dict)

def load_openapi_spec(url):
    try:
        openapi_spec = {} #defaultdicttree()
        url_parsed = urlparse(url)
        if url_parsed.scheme in ['http', 'https']:
            swagger = requests.get(url).json()
        else: # if url_parsed.scheme == ''
            # treat as file on disk
            with open(url, "rb") as f:
                swagger = json.load(f)

        # swagger will now contain json

        openapi_spec['paths'] = {}
        for path in swagger['paths']:
            # replace wildcards in {varname} format to a named regex
            path_regex = re.sub("{([^}]+)}", "(?P<\\1>[^/]+)", path).rstrip('/')
            if path_regex.endswith("proxy"):
                path_regex += ".*$"
            else:
                path_regex += "$"
            # use the path regex as the key so that we search for a match easily
            openapi_spec['paths'][path_regex] = {}
            openapi_spec['paths'][path_regex]['path'] = path
            # get the level (alpha/beta/stable) and the version from the path
            m = re.search("/v(?P<api_version>[0-9]+)(?:(?P<api_level>alpha|beta)(?P<api_level_version>[0-9]+))?", path)
            if m:
                extract = m.groupdict()

                level = extract.get("api_level")
                if level is None:
                    level = "stable"
                openapi_spec['paths'][path_regex]['level'] = level
                openapi_spec['paths'][path_regex]['version'] = extract["api_version"]
            # methods
            openapi_spec['paths'][path_regex]['methods'] = {}
            methods = swagger['paths'][path].keys()
            for method in methods:
                if method == "parameters":
                    continue
                openapi_spec['paths'][path_regex]['methods'][method] = {}
                # todo - request + response

            # crazy caching using prefixes
            bits = path.strip("/").split("/", 2)
            if bits[0] in ["apis", "api"] and len(bits) > 1:
                prefix_cache["/" + "/".join(bits[0:2])][path_regex] = openapi_spec['paths'][path_regex] 
            else:
                prefix_cache[None][path_regex] = openapi_spec['paths'][path_regex]
            # print path, path_regex, re.match(path_regex, path.rstrip('/')) is not None
        print prefix_cache.keys()
        return openapi_spec

    except Exception as e:
        print("Failed to load openapi spec \"%s\"" % url)
        raise e

def load_audit_log(path):
    audit_log = []
    with open(path, "rb") as logfile:
        for entry in logfile:
            raw_event = json.loads(entry)
            # change verb to represent http request
            verb_tt = {
                'get': ['get', 'list', 'watch', 'proxy'],
                'put': ['update', 'patch'],
                'post': ['create'],
                'delete': ['delete', 'deletecollection']
            }

            for method, verbs in verb_tt.items():
                if raw_event['verb'] in verbs:
                    raw_event['method'] = method
                    break
            if "method" not in raw_event:
                print("Error parsing event - HTTP method map not defined at \"%s\" for verb \"%s\"" % (raw_event['requestURI'], raw_event['verb']))
                # print(raw_event)

            audit_log.append(raw_event)
    return audit_log

def generate_count_tree(openapi_spec):

    count_tree = {}

    for endpoint in openapi_spec['paths'].values():
        path = endpoint['path']
        count_tree[path] = {
            "counter": 0,
            "methods": {},
            "level": endpoint.get("level")
        }
        for method in endpoint['methods']:
            count_tree[path]['methods'][method] = {
                "counter": 0,
                "fields": {}
            }

    return count_tree

search_cache = {}

def find_openapi_entry(openapi_spec, event):
    url = urlparse(event['requestURI'])
    # 1) Cached seen before results
    if url.path in search_cache:
        return search_cache[url.path]
    # 2) Indexed by prefix patterns to cut down search time
    for prefix in prefix_cache:
        if prefix is not None and url.path.startswith(prefix):
            # print prefix, url.path
            paths = prefix_cache[prefix]
            break
    else:
        paths = prefix_cache[None]
    
    for regex in paths:
        if re.match(regex, url.path):
            search_cache[url.path] = openapi_spec['paths'][regex]
            return openapi_spec['paths'][regex]
        elif re.search(regex, event['requestURI']):
            print("Incomplete match", regex, event['requestURI'])
    # cache failures too
    search_cache[url.path] = None
    return None

def count_event(count_tree, event, spec_entry):
    path = spec_entry['path']
    method = event['method']
    count_tree[path]['counter'] += 1
    try:
        count_tree[path]['methods'][method]['counter'] += 1
    except:
        print method, event['requestURI'], path, count_tree[path]['methods'].keys(), event['verb']
        raise

def get_count_results(count_tree):
    by_url = []
    by_url_and_method = []

    for url, url_data in count_tree.items():
        by_url += [(url_data['level'] or '-', url, url_data['counter'])]
        for method, method_data in url_data['methods'].items():
            by_url_and_method += [(url_data['level'] or '-', url, method, method_data['counter'])]

    results = {
        "by_url": sorted(by_url, key=lambda x: (-x[-1], x[0], x[1])),
        "by_url_and_method": sorted(by_url_and_method, key=lambda x: (-x[-1], x[0], x[1], x[2]))
    }
    return results

def generate_coverage_report(openapi_spec, audit_log):

    # need a tree of counters
    # <url>
    # - counter
    # - methods
    #   - <method>
    #     - counter
    #     - fields
    #       - <field>
    #         - counter
    count_tree = generate_count_tree(openapi_spec)
    event_summary = []
    unknown_urls = []
    unknown_url_methods = []
    for event in audit_log:
        spec_entry = find_openapi_entry(openapi_spec, event)
        if spec_entry is None:
            print("Entry not found for event URL \"%s\"" % event['requestURI'])
            unknown_urls += [event['requestURI']]
            continue
        try:
            count_event(count_tree, event, spec_entry)
        except:
            unknown_url_methods += [(event['requestURI'], event['method'], event['verb'])]
            continue
        event_summary.append([
            event['timestamp'],
            event['requestURI'],
            spec_entry['path'],
            event['method'],
            spec_entry.get('version', '-'),
            spec_entry.get('level'),
            ", ".join(event.get('sourceIPs', []))])

    count_results = get_count_results(count_tree)
    count_results['summary_log'] = sorted(event_summary, key=lambda x: x[0])
    count_results['unknown_urls'] = sorted(unknown_urls)

    hit = len(filter(lambda x: x[-1] > 0, count_results['by_url']))
    nohit = len(filter(lambda x: x[-1] == 0, count_results['by_url']))
    count_results['summary_endpoint_total'] = [[hit, nohit, hit + nohit, "%.2f" % (100.0 * hit / nohit)]]
    
    hit = len(filter(lambda x: x[-1] > 0, count_results['by_url_and_method']))
    nohit = len(filter(lambda x: x[-1] == 0, count_results['by_url_and_method']))
    count_results['summary_method_total'] = [[hit, nohit, hit + nohit, "%.2f" % (100.0 * hit / nohit)]]

    stats_endpoints = {}
    stats_methods = {}

    levels = sorted(set([x[0] for x in count_results['by_url']]))
    for level in levels:
        hit = len(filter(lambda x: x[0] == level and x[-1] > 0, count_results['by_url']))
        nohit = len(filter(lambda x: x[0] == level and x[-1] == 0, count_results['by_url']))
        stats_endpoints[level] = [level, hit, nohit, hit + nohit, "%.2f" % (100.0 * hit / nohit)]
        hit = len(filter(lambda x: x[0] == level and x[-1] > 0, count_results['by_url_and_method']))
        nohit = len(filter(lambda x: x[0] == level and x[-1] == 0, count_results['by_url_and_method']))
        stats_methods[level] = [level, hit, nohit, hit + nohit, "%.2f" % (100.0 * hit / nohit)]
    count_results['summary_endpoint_by_level'] = sorted(stats_endpoints.values(), key=lambda x: x[0])
    count_results['summary_method_by_level'] = sorted(stats_methods.values(), key=lambda x: x[0])
    count_results['unknown_urls'] = set(unknown_urls)
    count_results['unknown_methods'] = set([" | ".join(x) for x in unknown_url_methods])
    return count_results



def print_table(table, headers, title):
    print("===  %s  ===" % title)
    print("")

    field_lengths = [len(h) for h in headers]

    for row in table:
        for idx in range(len(field_lengths)):
            field_lengths[idx] = max(len(str(row[idx])), field_lengths[idx])

    line = []
    for idx, length in enumerate(field_lengths):
        line += [headers[idx].ljust(length)]
    print(" ".join(line))
    for row in table:
        line = []
        for idx, length in enumerate(field_lengths):
            line += [str(row[idx]).ljust(length)]
        print(" ".join(line))
    print("")

def print_report(report):
    print_table(filter(lambda x: x[-1] > 0, report['by_url']), ["LEVEL", "ENDPOINT", "COUNT"], "Hit counts by URL")
    print_table(filter(lambda x: x[-1] > 0, report['by_url_and_method']), ['LEVEL', 'ENDPOINT', 'METHOD', 'COUNT'], "Hit counts by URL and method")
    print_table(report['summary_endpoint_by_level'], ['LEVEL', 'HIT', 'UNHIT', 'TOTAL', '%'], "Endpoint coverage by level (alpha / beta / stable)")
    print_table(report['summary_endpoint_total'], ['HIT', 'UNHIT', 'TOTAL', '%'], "Endpoint coverage overall")
    print_table(report['summary_method_by_level'], ['LEVEL', 'HIT', 'UNHIT', 'TOTAL', '%'], "Endpoint / method coverage by level (alpha / beta / stable)")
    print_table(report['summary_method_total'], ['HIT', 'UNHIT', 'TOTAL', '%'], "Endpoint / method coverage overall")

    for item in report['unknown_urls']:
        print item

def main():
    # load swagger api file
    openapi_spec_url = "swagger.json"
    openapi_spec = load_openapi_spec(openapi_spec_url)
    #pprint(openapi_spec)
    
    # load audit log file
    audit_log_path = sys.argv[1]
    audit_log = load_audit_log(audit_log_path)
    # pprint(audit_log)
    # generate coverage report
    report = generate_coverage_report(openapi_spec, audit_log)

    print_report(report)

if __name__ == "__main__":
    main()