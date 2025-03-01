import json
import uuid
import hashlib
from flask import Flask, jsonify, request, make_response
from localstack.services import generic_proxy
from localstack.utils.aws import aws_stack
from localstack.utils.common import to_str, to_bytes
from localstack.utils.analytics import event_publisher

APP_NAME = 'ddb_streams_api'

app = Flask(APP_NAME)

DDB_STREAMS = {}

DDB_KINESIS_STREAM_NAME_PREFIX = '__ddb_stream_'

ACTION_HEADER_PREFIX = 'DynamoDBStreams_20120810'

SEQUENCE_NUMBER_COUNTER = 1


def add_dynamodb_stream(table_name, latest_stream_label=None, view_type='NEW_AND_OLD_IMAGES', enabled=True):
    if enabled:
        # create kinesis stream as a backend
        stream_name = get_kinesis_stream_name(table_name)
        aws_stack.create_kinesis_stream(stream_name)
        latest_stream_label = latest_stream_label or 'latest'
        stream = {
            'StreamArn': aws_stack.dynamodb_stream_arn(
                table_name=table_name, latest_stream_label=latest_stream_label),
            'TableName': table_name,
            'StreamLabel': latest_stream_label,
            'StreamStatus': 'ENABLED',
            'KeySchema': [],
            'Shards': []
        }
        table_arn = aws_stack.dynamodb_table_arn(table_name)
        DDB_STREAMS[table_arn] = stream
        # record event
        event_publisher.fire_event(event_publisher.EVENT_DYNAMODB_CREATE_STREAM,
            payload={'n': event_publisher.get_hash(table_name)})


def forward_events(records):
    global SEQUENCE_NUMBER_COUNTER
    kinesis = aws_stack.connect_to_service('kinesis')
    for record in records:
        if 'SequenceNumber' not in record['dynamodb']:
            record['dynamodb']['SequenceNumber'] = str(SEQUENCE_NUMBER_COUNTER)
            SEQUENCE_NUMBER_COUNTER += 1
        table_arn = record['eventSourceARN']
        stream = DDB_STREAMS.get(table_arn)
        if stream:
            table_name = table_name_from_stream_arn(stream['StreamArn'])
            stream_name = get_kinesis_stream_name(table_name)
            kinesis.put_record(StreamName=stream_name, Data=json.dumps(record), PartitionKey='TODO')


@app.route('/', methods=['POST'])
def post_request():
    action = request.headers.get('x-amz-target')
    data = json.loads(to_str(request.data))
    result = {}
    kinesis = aws_stack.connect_to_service('kinesis')
    if action == '%s.ListStreams' % ACTION_HEADER_PREFIX:
        result = {
            'Streams': list(DDB_STREAMS.values()),
            'LastEvaluatedStreamArn': 'TODO'
        }
    elif action == '%s.DescribeStream' % ACTION_HEADER_PREFIX:
        for stream in DDB_STREAMS.values():
            if stream['StreamArn'] == data['StreamArn']:
                result = {
                    'StreamDescription': stream
                }
                # get stream details
                dynamodb = aws_stack.connect_to_service('dynamodb')
                table_name = table_name_from_stream_arn(stream['StreamArn'])
                stream_name = get_kinesis_stream_name(table_name)
                stream_details = kinesis.describe_stream(StreamName=stream_name)
                table_details = dynamodb.describe_table(TableName=table_name)
                stream['KeySchema'] = table_details['Table']['KeySchema']

                # Replace Kinesis ShardIDs with ones that mimic actual
                # DynamoDBStream ShardIDs.
                stream_shards = stream_details['StreamDescription']['Shards']
                for shard in stream_shards:
                    shard['ShardId'] = shard_id(stream_name, shard['ShardId'])
                stream['Shards'] = stream_shards
                break
        if not result:
            return error_response('Requested resource not found', error_type='ResourceNotFoundException')
    elif action == '%s.GetShardIterator' % ACTION_HEADER_PREFIX:
        # forward request to Kinesis API
        stream_name = stream_name_from_stream_arn(data['StreamArn'])
        stream_shard_id = kinesis_shard_id(data['ShardId'])
        result = kinesis.get_shard_iterator(StreamName=stream_name,
            ShardId=stream_shard_id, ShardIteratorType=data['ShardIteratorType'])
    elif action == '%s.GetRecords' % ACTION_HEADER_PREFIX:
        kinesis_records = kinesis.get_records(**data)
        result = {'Records': [], 'NextShardIterator': kinesis_records.get('NextShardIterator')}
        for record in kinesis_records['Records']:
            result['Records'].append(json.loads(to_str(record['Data'])))
    else:
        print('WARNING: Unknown operation "%s"' % action)
    return jsonify(result)


# -----------------
# HELPER FUNCTIONS
# -----------------

def error_response(message=None, error_type=None, code=400):
    if not message:
        message = 'Unknown error'
    if not error_type:
        error_type = 'UnknownError'
    if 'com.amazonaws.dynamodb' not in error_type:
        error_type = 'com.amazonaws.dynamodb.v20120810#%s' % error_type
    content = {
        'message': message,
        '__type': error_type
    }
    return make_response(jsonify(content), code)


def get_kinesis_stream_name(table_name):
    return DDB_KINESIS_STREAM_NAME_PREFIX + table_name


def table_name_from_stream_arn(stream_arn):
    return stream_arn.split(':table/')[1].split('/')[0]


def stream_name_from_stream_arn(stream_arn):
    table_name = table_name_from_stream_arn(stream_arn)
    return get_kinesis_stream_name(table_name)


def random_id(stream_arn, kinesis_shard_id):
    namespace = uuid.UUID(bytes=hashlib.sha1(to_bytes(stream_arn)).digest()[:16])
    return uuid.uuid5(namespace, to_bytes(kinesis_shard_id)).hex


def shard_id(stream_arn, kinesis_shard_id):
    return '-'.join([kinesis_shard_id, random_id(stream_arn, kinesis_shard_id)])


def kinesis_shard_id(dynamodbstream_shard_id):
    return dynamodbstream_shard_id.rsplit('-', 1)[0]


def serve(port, quiet=True):
    generic_proxy.serve_flask_app(app=app, port=port, quiet=quiet)
