import json
import base64
import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth
import requests
import streamlit as st
from PIL import Image
from io import BytesIO

st.set_page_config(page_title="Damage Repair Cost Estimator") #HTML title
st.title("Damage Repair Cost Estimator") #page title

from botocore.config import Config

config = Config(
   retries = {
      'max_attempts': 10,
      'mode': 'adaptive'
   }
)

# Create a Boto3 STS client
sts_client = boto3.client('sts')
# Call the assume_role method of the STSConnection object and pass the role
#Starts the Session with the assumed role
session = boto3.Session()

#Get SSM Parameter values for OpenSearch Domain and
ssm = session.client('ssm')
parameters = ['/car-repair/collection-domain-name', '/car-repair/distribution-domain-name'] 
response = ssm.get_parameters(
    Names=parameters,
    WithDecryption=True
)
#Set OpenSearch Details
parameter1_value = response['Parameters'][0]['Value']
coll_domain_name = parameter1_value[8:]
os_host = coll_domain_name #collection host name from the cloudformation template. DO NOT ADD https://
os_index_name = 'repair-cost-data' #os index name that will be created

#set cloudfront url
parameter2_value = response['Parameters'][1]['Value']
cf_url = parameter2_value #get this from the CloudFormation template that created the CloudFront distribution

#Initialize OpenSearch Client
credentials = session.get_credentials()
client = session.client('opensearchserverless')
service = 'aoss'
region = session.region_name
awsauth = AWS4Auth(credentials.access_key, credentials.secret_key,
                   region, service, session_token=credentials.token)

# Area, Type and Severity Options
damage_area_options = ['Hood', 'Rear Left', 'Rear Right', 'Front Left', 'Front Right', 'Wheel', " Driver Side", 'Passenger Side', " Driver side Door", 'Passenger Side Door', "Windshield"]
damage_type_options = ['Scratch', 'Dent', 'Fender Bender', "Broken"]
damage_sev_option = ['light', 'moderate', 'severe', 'major']

options = ['Make_1', 'Make_2', 'Make_3']

# initialize session_state of fields
if "selected" not in st.session_state:
    st.session_state.selected = []
if "selected_make" not in st.session_state:
    st.session_state.selected_make = None
if "selected_damage_type" not in st.session_state:
    st.session_state.selected_damage_type = 0
if "selected_damage_sev" not in st.session_state:
    st.session_state.selected_damage_sev = None
if "similar_images" not in st.session_state:
    st.session_state.similar_images = []
if "damage_description" not in st.session_state:
    st.session_state.damage_description = ""
if "relevance" not in st.session_state:
    st.session_state.relevance = None
    

st.session_state.selected = st.sidebar.selectbox('Select Car Make', options)

if "messages" not in st.session_state:
    st.session_state.messages = []

# Make and Model options
if st.session_state.selected == 'Make_1':
    second_options = ['Model_1']
elif st.session_state.selected == 'Make_2':
    second_options = ['Model_2'] 
else:
    st.session_state.selected = ['Make_3']
    second_options = ['Model_3']

# Select boxes for all options
st.session_state.selected_make = st.sidebar.selectbox('Second Make', second_options)
st.session_state.selected_damage_area = st.sidebar.multiselect('Damage Area. Select as many parts as possible that might be involed on this damage:', damage_area_options)
st.session_state.selected_damage_type = st.sidebar.multiselect('Damage Type. Select as many damage types as possible that can describe the damage: ', damage_type_options)
st.session_state.selected_damage_sev = st.sidebar.selectbox('Damage Severity', damage_sev_option)

matches = ['1', '2', '3']

number_of_matches = st.sidebar.selectbox('Number of matches from the OS Vector DB to match with the current image. Max of 3', matches)
# session state is valid through out the lifecycle of the app until the whole page is reloaded. we initialize the key to 0 here
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0
if "uploaded_file_data" not in st.session_state:
    st.session_state.uploaded_file_data = None  # Stores the file data

#dynamically generate the key for the uploaded file
upload_file = st.sidebar.file_uploader("here you will upload your damage image", key=st.session_state.uploader_key)

def response_streaming(invoke_body):
    st.write('**Streaming the Final Calculation**')
    response = client.invoke_model_with_response_stream(
        body=invoke_body,
        contentType='application/json',
        accept='application/json',
        modelId='anthropic.claude-3-haiku-20240307-v1:0'
    )
    for event in response.get("body"):
        # loading the json, and accessing the specific bytes
        chunk = json.loads(event["chunk"]["bytes"])
        # If a message_delta is detected, it prints the stop reason, stop sequence, and output tokens.
        if chunk['type'] == 'message_delta':
            print(f"\nStop reason: {chunk['delta']['stop_reason']}")
            print(f"Stop sequence: {chunk['delta']['stop_sequence']}")
            print(f"Output tokens: {chunk['usage']['output_tokens']}")
        #  If a content_block_delta is detected, it determines if it includes a text_delta.
        if chunk['type'] == 'content_block_delta':
            # If a text_delta is detected, it streams the text to the front end.
            if chunk['delta']['type'] == 'text_delta':
                # using a generator object to stream the text to the front end.
                #yield chunk['delta']['text']
                yield chunk['delta']['text'].replace("$", "\$")


def check_vehicle_presence(image_bytes):
    rekognition_client = boto3.client("rekognition")
    response = rekognition_client.detect_labels(Image={"Bytes": image_bytes})
    labels = [label["Name"] for label in response["Labels"]]
    print(labels)
    
    if "Car"  in labels:
         return True, "Car detected"
    elif "car"  in labels:
         return True, "Car detected"
    else:
         return False, "No Car detected"


if upload_file is not None:
    #once the file is consumed, first time then update the uploader_key, so that this piece of code will not be triggered again.
    st.session_state.uploaded_file_data = {
        "filename": upload_file.name,
        "filetype": upload_file.type,
        "filesize": upload_file.size,
        "content": base64.b64encode(upload_file.read()).decode(),  # Store encoded file data
    }
    
    st.session_state.uploader_key += 1
    
    file_details = {"filename":upload_file.name, "filetype":upload_file.type, "filesize":upload_file.size}
#Reads the file and encodes it
    upload_file.seek(0)  # Reset pointer to beginning
    file_bytes = upload_file.read()

    if not file_bytes:  # Check if file_bytes is empty
        st.error("Uploaded file is empty. Please upload a valid image.")
        st.stop()
    
    # base64_bytes = file_bytes.encode('ascii')
    # image_bytes = base64.b64decode(base64_bytes)
    
    s3_client = boto3.client("s3")
    BUCKET_NAME = "uploaded-images-bucket-for-blog"
    FILE_NAME = upload_file.name
    
    s3_client.put_object(
        Bucket=BUCKET_NAME,
        Key=FILE_NAME,
        Body=file_bytes,
        ContentType="image/jpeg"
    )

    
    vehicle_present, message = check_vehicle_presence(file_bytes)
    if not vehicle_present:
        st.error("No car detected in the uploaded image. Please upload an image containing a car.")
    else:
        encoded_image = base64.b64encode(file_bytes).decode()
    #Creates the JSON metadata based on the options selected by the user
        json_text = {
        "make": st.session_state.selected,
        "model": st.session_state.selected_make,
        "state": "FL",
        "damage": st.session_state.selected_damage_area,
        "damage_severity": st.session_state.selected_damage_sev,
        "damage_type": st.session_state.selected_damage_type
        }
    #Turns the string into json and encodes it into base64 encoded bytes
        json_string = json.dumps(json_text)
        #st.write(json.loads(json_string))
        base64_bytes = json_string.encode('utf-8')
        base64_string = base64.b64encode(base64_bytes).decode('utf-8')
    #created bedrock client
        bedrock = boto3.client('bedrock-runtime', config=config) 

    #Here we create the instruction that will be sent to Claude 3 to improve upon the metadata created just by the user Options. We will create a damage description
        json_model = '<model>\
        {\
        "make": "XXXXX",\
        "model": "XXXXX",\
        "state": "FL",\
        "damage": "Front and Rear",\
        "damage_severity": "moderate",\
        "damage_description": "Front and rear bumper cover repairs"\
        }</model\
        '
        real_data_json = '<real_data>' + str(json_text) + '</real_data>'
        prompt_description = json_model + real_data_json + 'Instruction: You are a car damage assessor that needs to create a short description for the damage in the image. Analyze the image and populate the json output adding an extra field called damage_description, this description has to be short and less than 10 words, provide ONLY the json as a response and no other data, the xml tags also must not be in the response.'
        invoke_body = {
        'anthropic_version': 'bedrock-2023-05-31',
        'max_tokens': 2000,
        'temperature': 1,
        'top_p': 1,
        'top_k': 250,
        'messages': [
            {
            'role': 'user',
            'content': [
                {
                "type": "image",
                "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": encoded_image
                }
            },
            {
                "type": "text",
                "text": prompt_description
            }
                ]
            }   
        ]
        }
        invoke_body = json.dumps(invoke_body).encode('utf-8')
        client = session.client('bedrock-runtime', config=config)
    #here is where we invoke Claude and take its response.
        response = client.invoke_model(
            body=invoke_body,
            contentType='application/json',
            accept='application/json',
            modelId='anthropic.claude-3-haiku-20240307-v1:0'
        )
        response_body = response['body'].read()
        data = json.loads(response_body)
        text = data['content'][0]['text']

        json_string = json.dumps(text)
        data_2 = json.loads(json_string)
        st.session_state.damage_description = json.loads(text)["damage_description"]
        #st.write('JSON output Created by Claude 3 Haiku:')
        #st.write(text)
        json_bytes = data_2.encode('utf-8') 
        base64_bytes = base64.b64encode(json_bytes)
        encoded_json = base64_bytes.decode('utf-8')


    #here we create the body with the image and the JSON output from Claude 3 and send it to Titan to create the vector.
        body = json.dumps({
                "inputImage": encoded_image,
                "inputText": encoded_json,
                "embeddingConfig": {
                    "outputEmbeddingLength": 1024
                }
            })

        # Invoke Titan Multimodal Embeddings model
        response = bedrock.invoke_model(
        body=body,
        modelId="amazon.titan-embed-image-v1",
        accept="application/json",
        contentType="application/json"
        )
        embedding = response['body']
        body_output = embedding.read()
        body_string = body_output.decode('utf-8')
        data_embedded = json.loads(body_string)  
        image_vector = data_embedded['embedding']
        json_embedding = json.loads(body_string)
        params = {"size": number_of_matches} 

        # Build search body with kNN query, with the vector created by Titan
        body = {
        "query": {
            "knn": {
            "damage_vector": {
                "vector": image_vector,
                "k": number_of_matches
            }
            }
        }
        }

        # Encode body to JSON
        body = json.dumps(body)
        headers = {'Content-Type': 'application/json; charset=utf-8'}

        # Send search request
        
        url = f"https://{os_host}/_search"
        response = requests.get(url, 
                                auth=awsauth,
                                params=params,
                                data=body,
                                headers=headers)
        results = response.json()
        num_results = len(results['hits']['hits'])
        columns = st.columns(num_results + 1)

        metadata_strings = []

        with columns[0]:
            st.write('This is the Image that has been provided: ')
            current_img = Image.open(BytesIO(file_bytes))
            st.image(file_bytes)

        for i, hit in enumerate(results['hits']['hits']):
            metadata = hit['_source']['metadata']
            s3_location = metadata['s3_location']
            print(s3_location)
            st.session_state.similar_images.append(s3_location)
            score = hit['_score']
            metadata_string = json.dumps(metadata, indent=2)  # Convert metadata to JSON string
            metadata_strings.append(metadata_string)  # Append the metadata string to the list
            with columns[i + 1]:
                url = 'https://' + cf_url + '/' + s3_location
                response = requests.get(url)
                img = Image.open(BytesIO(response.content))
                st.write(f'This is the Match Accuracy for Image {i + 1}: {score}')
                st.sidebar.write(f'This is the metadata for the closest match we have in our DataStore for Image {i + 1}')
                st.sidebar.code(json.dumps(metadata, indent=2)) 

                st.image(img)
        combined_metadata_string = '\n'.join(metadata_strings)
        prompt_full = '<current>' + json_string + '</current>' + '<dataset>' + combined_metadata_string + '</dataset> Instruction; You are calculating the estimated repair cost based on previous data of similar car damages. Take the repair cost of the data set provide within <dataset> and calculate the average cost among all example data sets. And you also need to provide a recommended service provider name from the dataset provided within <dataset> based on the state in which car is damaged and it should be closest one. Explain the math, but you must be brief, and the service provider name should be in next line with the sentence "Recommended Service Provider Name: <service-provider-name>", the answer cannot have more than 3 sentences.'
        
        invoke_body = {
        'anthropic_version': 'bedrock-2023-05-31',
        'max_tokens': 1000,
        'messages': [
            {
            'role': 'user',
            'content': [
                {
                    'type': 'text',
                    'text': prompt_full
                }
                ]
            }
        ]
        }
        invoke_body = json.dumps(invoke_body).encode('utf-8')

        answer = st.write_stream(response_streaming(invoke_body))

        st.session_state.messages.append({"role": "assistant",
                                            "content": answer})
        
        col1, col2 = st.columns(2)

        # go to next page for user feedback
        with col1:
            if st.button("👍 Thumbs Up"):
                st.session_state.relevance = "positive"
                st.query_params["relevance"] = "positive"
                st.switch_page("pages/feedback.py")

        with col2:
            if st.button("👎 Thumbs Down"):
                st.session_state.relevance = "negative"
                st.query_params["relevance"] = "negative"
                st.switch_page("pages/feedback.py")   