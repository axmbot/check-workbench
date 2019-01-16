import pandas as pd
import json
import numpy as np
import aiohttp
from collections import OrderedDict
import os.path
import base64

class CheckError(Exception):
  pass

# https://stackoverflow.com/a/43621819/209184
def dict_get(_dict, keys, default=None):
  for key in keys:
    if isinstance(_dict, dict):
      _dict = _dict.get(key, default)
    else:
      return default
  return _dict

def parse_date(date_string, default=None):
  try:
    return pd.Timestamp.strptime(date_string, '%Y-%m-%dT%H:%M:%S.000Z')
  except ValueError:
    return default

def array_reverse(_array):
  return _array[::-1]

async def query(params):
  # Use the API key to perform a query on the Check API.
  project = base64.encodestring('Project/{0}'.format(os.path.split(params['project'])[1]).encode()).decode('utf-8')
  key = params['key'].strip()
  host = params['host'].strip()
  cursor = ''
  page = 20
  query = """
query {
  node(id: "%(project)s") {
    ...F0
  }
} fragment F0 on Project {
  id
  dbid
  title
  project_medias(first: %(page)d, after: "%(cursor)s") {
    pageInfo {
      hasNextPage
      startCursor
      hasPreviousPage
      endCursor
    }
    edges { cursor node {
      user {
        id
        name
      }
      id
      dbid
      created_at
      report_type
      metadata
      last_status
      media {
        quote
        picture
        url
        embed
      }
      tags { edges { node {
        tag_text
      }}}
      tasks { edges { node {
        annotator {
          user {
            id
            name
          }
        }
        created_at
        label
        status
        first_response {
          annotator {
            user {
              id
              name
            }
          }
          created_at
          content
        }
        log { edges { node {
          annotation {
            annotator {
              user {
                id
                name
              }
            }
            created_at
            content
          }
          event_type
        }}}
      }}}
      comments: annotations(annotation_type: "comment") { edges { node {
        annotator {
          user {
            id
            name
          }
        }
        created_at
        content
      }}}
      log { edges { node {
        created_at
        user {
          id
        }
        event_type
      }}}
    }}
  }
}
"""
  async with aiohttp.ClientSession(headers={ 'X-Check-Token': key }) as session:
    data = None
    while True:
      request = { 'query': query % { 'project': project, 'page': page, 'cursor': cursor } }
      async with session.post(host + '/api/graphql', data=request) as response:
        d = await response.json()
        if (d.get('error')):
          raise CheckError(d['error'])
        if (d.get('errors')):
          raise CheckError(d['errors'][0]['message'])
        # Accumulate results into `data`.
        if data == None:
          data = dict(d)
        else:
          data['data']['node']['project_medias']['edges'] += d['data']['node']['project_medias']['edges']
        # Next page or exit
        if (d['data']['node']['project_medias']['pageInfo']['hasNextPage']):
          cursor = d['data']['node']['project_medias']['pageInfo']['endCursor']
        else:
          break
    return data

def media_time_to_status(media, first=True):
  times = list(map(lambda l: l['node']['created_at'], [l for l in array_reverse(media['node']['log']['edges']) if l['node']['event_type'] == 'update_dynamicannotationfield']))
  if len(times) == 0:
    return None
  time = times[0] if first else times[-1]
  return pd.Timedelta(seconds=(int(time) - int(media['node']['created_at'])))

def format_comments(comments):
  if len(comments) == 0:
    return None
  if len(comments) > 1:
    comments[0] = '- ' + comments[0]
  return '\n- '.join(comments)

def task_comments(task):
  return format_comments(
    list(map(
      lambda l: json.loads(l['node']['annotation']['content'])['text'],
      [l for l in task['log']['edges'] if l['node']['event_type'] == 'create_comment']
    ))
  )

def media_comments(media):
  return format_comments(
    list(map(
      lambda c: json.loads(c['node']['content'])['text'],
      media['node']['comments']['edges']
    ))
  )

def task_answer(task):
  content = json.loads(task['first_response']['content'])
  for field in content:
    if field['field_name'].startswith('response_'):
      return field['formatted_value']
  return None

def media_tags(media):
  tags = array_reverse(media['node']['tags']['edges'])
  if len(tags) == 0:
    return None
  return ', '.join(map(lambda t: t['node']['tag_text'], tags))

def format_user(user, anonymize):
  return 'Anonymous' if anonymize else user['name']

def flatten(data):
  # Convert the GraphQL result to a Pandas DataFrame.
  df = []
  project = data['data']['node']
  for media in project['project_medias']['edges']:
    metadata = json.loads(media['node']['metadata'])
    base = OrderedDict({
      'project': project['title'],
      'identifier': str(media['node']['dbid']),
      'title': metadata['title'],
      'added_by': format_user(media['node']['user'], False),
      'added_by_anon': format_user(media['node']['user'], True),
      'date_added': pd.Timestamp.fromtimestamp(int(media['node']['created_at'])),
      'status': media['node']['last_status'],
      'content': media['node']['media']['quote'] if media['node']['report_type'] == 'claim' else metadata['description'],
      'url': {
        'uploadedimage': media['node']['media']['picture'],
        'link': media['node']['media']['url']
      }.get(media['node']['report_type']),
      'type': media['node']['media']['embed']['provider'] if media['node']['report_type'] == 'link' else media['node']['report_type'],
      'date_published': parse_date(dict_get(media, ['node', 'media', 'embed', 'published_at'], '')),
      'tags': media_tags(media),
      'comments': media_comments(media),
      'count_contributors': np.unique(map(lambda l: l['node']['user']['id'], media['node']['log']['edges'])).size,
      'count_notes': len(media['node']['comments']['edges']),
      'count_tasks': len(media['node']['tasks']['edges']),
      'count_tasks_completed': len([t for t in media['node']['tasks']['edges'] if t['node']['status'] == 'resolved']),
      'time_to_first_status': media_time_to_status(media, True),
      'time_to_last_status': media_time_to_status(media, False)
    })
    if base['count_tasks'] == 0:
      df.append(base)
    else:
      for i, task in enumerate(array_reverse(media['node']['tasks']['edges'])):
        item = base.copy()
        item['task'] = i+1
        item['task_question'] = task['node']['label']
        item['task_comments'] = task_comments(task['node'])
        item['task_added_by'] = format_user(task['node']['annotator']['user'], False)
        item['task_added_by_anon'] = format_user(task['node']['annotator']['user'], True)
        if task['node']['first_response']:
          item['task_answer'] = task_answer(task['node'])
          item['task_date_answered'] = pd.Timestamp.fromtimestamp(int(media['node']['created_at']))
          item['task_answered_by'] = format_user(task['node']['first_response']['annotator']['user'], False)
          item['task_answered_by_anon'] = format_user(task['node']['first_response']['annotator']['user'], True)
        df.append(item)
  return pd.DataFrame(df)

async def fetch(params, **kwargs):
  try:
    return flatten(await query(params))
  except Exception as err:
    return '%(ex)s: %(err)s' % { 'ex': err.__class__.__name__, 'err': str(err) }

def render(table, params, *, fetch_result, **kwargs):
  if fetch_result is None:
    return fetch_result
  if fetch_result.status == 'error':
    return fetch_result
  if fetch_result.dataframe.empty:
    return fetch_result

  columns = [c for c in list(fetch_result.dataframe) if c.endswith('_anon')]
  for c in columns:
    if params['anonymize']:
      del fetch_result.dataframe[c.replace('_anon', '')]
    else:
      del fetch_result.dataframe[c]
  return fetch_result
