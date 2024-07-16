from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from airflow.models import Variable
#from airflow.provider.amazon.operators.s3 import S3CreateObjectOperator
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from io import StringIO
from airflow.providers.amazon.aws.operators.s3 import S3CreateObjectOperator

from datetime import datetime, timedelta
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import json
import pandas as pd

#define args
default_args = {
    "owner": "raunak",
    "depends_on_past": False,
    "start_date": datetime(2024, 7, 13),
}

def _fetch_spotify_data(**kwargs):
    date=datetime.now()
    client_id = Variable.get('spotify_client_id')
    client_secret = Variable.get('spotify_client_secret')

    client_credentials_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
    sp = spotipy.Spotify(client_credentials_manager=client_credentials_manager)

    playlist_link = "https://open.spotify.com/playlist/37i9dQZEVXbMDoHDwVN2tF"
    spotify_data = sp.playlist_tracks(playlist_link)
    
    file_name="spotify_raw_"+datetime.now().strftime("%Y%m%d%H%M%S")+".json"
    # with open(file_name,"wb") as f:
    #             f.write(spotify_data)

    kwargs['ti'].xcom_push(key="spotify_filename",value =file_name)
    kwargs['ti'].xcom_push(key="spotify_data",value =json.dumps(spotify_data))


def _read_data_from_s3(**kwargs):
    s3_hook=S3Hook(aws_conn_id="aws_spotify_conn")
    bucket_name= "spotify-etl-project-raunak"
    prefix="raw_data/to_processed/"
    keys= s3_hook.list_keys(bucket_name=bucket_name,prefix=prefix)

    spotify_data = []
    for key in keys:
        if key.endswith(".json"):
             data= s3_hook.read_key(key, bucket_name)
             spotify_data.append(json.loads(data))


    kwargs['ti'].xcom_push(key='spotify_data',value=spotify_data)

    print(spotify_data)
    
def _process_album(**kwargs):
    spotify_data=kwargs['ti'].xcom_pull(task_ids="read_data_from_s3",key="spotify_data")
    album_list=[]
    for data in spotify_data:
        for row in data['items']:
            album_id=row['track']['album']['id']
            album_name=row['track']['album']['name']
            release_date=row['track']['album']['release_date']
            total_tracks=row['track']['album']['total_tracks']
            album_url=row['track']['album']['external_urls']['spotify']
            album_dict={'album_id':album_id,
                        'album_name':album_name,
                        'release_date':release_date,
                        'total_tracks':total_tracks,
                        'album_url':album_url
                    }
            album_list.append(album_dict)

    album_df=pd.DataFrame.from_dict(album_list)
    album_df = album_df.drop_duplicates(subset=['album_id'])

    album_buffer =StringIO()
    album_df.to_csv(album_buffer, index=False)
    album_content=album_buffer.getvalue()
    kwargs['ti'].xcom_push(key='album_content',value=album_content)



def _process_artist(**kwargs):
    spotify_data=kwargs['ti'].xcom_pull(task_ids="read_data_from_s3",key="spotify_data")
    artist_list=[]
    for data in spotify_data:
        for row in data['items']:
            for key,value in row.items():
                if key== "track":
                    for artist in value['artists']:
                        artist_dict={'artist_id':artist['id'],'artist_name':artist['name'],'external_url':artist['href']}
                        artist_list.append(artist_dict)

    artist_df=pd.DataFrame.from_dict(artist_list)
    artist_df = artist_df.drop_duplicates(subset=['artist_id'])



    artist_buffer =StringIO()
    artist_df.to_csv(artist_buffer, index=False)
    artist_content=artist_buffer.getvalue()
    kwargs['ti'].xcom_push(key='artist_content',value=artist_content)
           
        


def _process_song(**kwargs):
    spotify_data=kwargs['ti'].xcom_pull(task_ids="read_data_from_s3",key="spotify_data")
    song_list=[]
    for data in spotify_data:
        for row in data['items']:
            song_id=row['track']['id']
            song_name=row['track']['name']
            song_duration=row['track']['duration_ms']
            song_url=row['track']['external_urls']['spotify']
            song_added=row['added_at']
            song_popularity=row['track']['popularity']
            album_id=row['track']['album']['id']
            artist_id=row['track']['album']['artists'][0]['id']
            song_element={'song_id':song_id,'song_name':song_name,'song_duration':song_duration,'song_popularity':song_popularity,
                        'song_url':song_url,'song_added':song_added,'album_id':album_id,'artist_id':artist_id}
            song_list.append(song_element)
        
    song_df=pd.DataFrame.from_dict(song_list)
    song_df = song_df.drop_duplicates(subset=['song_id'])

    song_buffer =StringIO()
    song_df.to_csv(song_buffer, index=False)
    song_content=song_buffer.getvalue()
    kwargs['ti'].xcom_push(key='song_content',value=song_content)

def _move_processed_data(**kwargs):
    s3_hook=S3Hook(aws_conn_id="aws_spotify_conn")
    bucket_name= "spotify-etl-project-raunak"
    prefix="raw_data/to_processed/"
    target_prefix = "raw_data/processed/"


    keys= s3_hook.list_keys(bucket_name=bucket_name,prefix=prefix)

    for key in keys:
        if key.endswith(".json"):
            new_key= key.replace(prefix,target_prefix)
            s3_hook.copy_object(
                source_bucket_key=key,
                dest_bucket_key=new_key, 
                source_bucket_name=bucket_name,
                dest_bucket_name=bucket_name
            )

            s3_hook.delete_objects(bucket=bucket_name,keys=key)
             
    



dag = DAG(
    dag_id="spotify_etl_dag",
    default_args=default_args,
    description="ETL Process for Spotify Data",
    schedule_interval=timedelta(days=1),
    catchup=False,
)

fetch_data = PythonOperator(
    task_id='fetch_spotify_data',
    python_callable=_fetch_spotify_data,  # Corrected this line
    provide_context=True,  # Added this line to pass context
    dag=dag,
)

store_raw_s3 =S3CreateObjectOperator(
    task_id='upload_raw_to_S3',
    aws_conn_id="aws_spotify_conn",
    s3_bucket='spotify-etl-project-raunak',
    s3_key="raw_data/to_processed/{{task_instance.xcom_pull(task_ids='fetch_spotify_data', key='spotify_filename') }}",
    data="{{task_instance.xcom_pull(task_ids='fetch_spotify_data', key='spotify_data') }}",
    replace=True,
    dag=dag,
)

read_data_from_s3 =PythonOperator(
    task_id='read_data_from_s3',
    python_callable=_read_data_from_s3,  # Corrected this line
    provide_context=True,  # Added this line to pass context
    dag=dag,
)


process_album=PythonOperator(
    task_id='process_album',
    python_callable=_process_album,  # Corrected this line
    provide_context=True,  # Added this line to pass context
    dag=dag,
    )

store_album_to_s3 =S3CreateObjectOperator(
    task_id='store_album_to_s3',
    aws_conn_id="aws_spotify_conn",
    s3_bucket='spotify-etl-project-raunak',
    s3_key="transformed_data/album_data/album_transformed_{{ts_nodash}}.csv",
    data="{{task_instance.xcom_pull(task_ids='process_album', key='album_content') }}",
    replace=True,
    dag=dag,
)



process_artist=PythonOperator(
    task_id='process_artist',
    python_callable=_process_artist,  # Corrected this line
    provide_context=True,  # Added this line to pass context
    dag=dag,
    )

store_artist_to_s3 =S3CreateObjectOperator(
    task_id='store_artist_to_s3',
    aws_conn_id="aws_spotify_conn",
    s3_bucket='spotify-etl-project-raunak',
    s3_key="transformed_data/artist_data/artist_transformed_{{ts_nodash}}.csv",
    data="{{task_instance.xcom_pull(task_ids='process_artist', key='artist_content') }}",
    replace=True,
    dag=dag,
)




process_song=PythonOperator(
    task_id='process_song',
    python_callable=_process_song,  # Corrected this line
    provide_context=True,  # Added this line to pass context
    dag=dag,
    )


store_song_to_s3 =S3CreateObjectOperator(
    task_id='store_song_to_s3',
    aws_conn_id="aws_spotify_conn",
    s3_bucket='spotify-etl-project-raunak',
    s3_key="transformed_data/song_data/song_transformed_{{ts_nodash}}.csv",
    data="{{task_instance.xcom_pull(task_ids='song_album', key='song_content')}}",
    replace=True,
    dag=dag,
)


move_processed_data_task=PythonOperator(
    task_id="move_processed_data_task",
    python_callable=_move_processed_data,
    provide_context=True,
    dag=dag,
)



fetch_data>>store_raw_s3

store_raw_s3>>read_data_from_s3 

read_data_from_s3>>process_album >> store_album_to_s3

read_data_from_s3>>process_song >> store_song_to_s3

read_data_from_s3>>process_artist >> store_artist_to_s3

store_album_to_s3>>move_processed_data_task

store_song_to_s3>>move_processed_data_task

store_artist_to_s3>>move_processed_data_task