import json
import os
from datetime import datetime, timedelta

import pandahouse as ph
import pandas as pd
from airflow.decorators import dag, task
from dotenv import load_dotenv

load_dotenv()


def extract_df(query):
    """Takes in a SQL query, returns pandas DF from ClickHouse server"""
    connection = json.loads(os.environ.get('DB_CONNECTION'))
    df = ph.read_clickhouse(query, connection=connection)
    return df


def get_age_bin(age):
    """Takes in an age number, returns age bin as string"""
    bins = {
        (0, 18): '0-18',
        (18, 25): '18-24',
        (25, 35): '25-34',
        (35, 45): '35-44',
        (45, 55): '45-54',
        (55, 65): '55-64'
    }
    for bounds, bin_ in bins.items():
        if bounds[0] <= age < bounds[1]:
            return bin_
    return '65+'


default_args = {
    'owner': 'a-tereshin',
    'depends_on_past': False,
    'retries': 3,
    'retry_delay': timedelta(seconds=300),
    'start_date': datetime(2022, 2, 2),
}
schedule_interval = '0 4 * * *'  # cron-expression (daily at 04:00 am)


@dag(default_args=default_args, schedule_interval=schedule_interval, catchup=False)
def atereshin_dag():
    @task()
    def extract_feed():
        # select query from feed actions
        query_feed = '''
           select 
                user_id,
                toDate(time) event_date,
                os, 
                if(gender = 1,'male','female') gender, 
                age,
                sum(action = 'like') likes,
                sum(action = 'view') views
           from {db}.feed_actions
           where toDate(time) = today() - 1
           group by user_id, event_date, age, gender, os
           '''
        df_feed = extract_df(query_feed)
        return df_feed

    @task()
    def extract_messenger():
        # select query from message actions
        query_message = '''
           with receivers as 
                (
                select 
                    user_id,
                    toDate(time) event_date,
                    count(distinct reciever_id) users_sent,
                    count(time) messages_sent, 
                    os,
                    if(gender = 1,'male','female') gender,
                    age
                from {db}.message_actions
                where toDate(time) = today() - 1
                group by user_id, event_date, os, gender, age
                ),
            senders as
                (
                select 
                    reciever_id user_id, 
                    count(distinct user_id) users_received, 
                    count(time) messages_received
                from {db}.message_actions
                where toDate(time) = today() - 1
                group by reciever_id
                )
            select * 
            from receivers
            left join senders
            using user_id
           '''
        df_message = extract_df(query_message)
        return df_message

    @task()
    def merge_extracted_tables(df1, df2):
        merged_df = df1.merge(df2, how='outer', on=['user_id', 'event_date', 'os', 'gender', 'age'])
        return merged_df

    @task()
    def os_aggregation(df):
        df_os = df.drop(['user_id', 'age'], axis=1).groupby(['event_date', 'os']).sum().reset_index()
        df_os['dimension'] = 'os'
        df_os.rename(columns={'os': 'dimension_value'}, inplace=True)
        return df_os

    @task()
    def gender_aggregation(df):
        df_gender = df.drop(['user_id', 'age'], axis=1).groupby(['event_date', 'gender']).sum().reset_index()
        df_gender['dimension'] = 'gender'
        df_gender.rename(columns={'gender': 'dimension_value'}, inplace=True)
        return df_gender

    @task()
    def age_aggregation(df):
        df_age = df.copy()
        df_age['age'] = df['age'].apply(get_age_bin)
        df_age = df_age.drop(['user_id'], axis=1).groupby(['event_date', 'age']).sum().reset_index()
        df_age['dimension'] = 'age'
        df_age.rename(columns={'age': 'dimension_value'}, inplace=True)
        return df_age

    @task
    def load(*args):
        # concatenation of dataframes with aggregated metrics
        df = pd.concat(args)
        # converting floats to ints in columns as like, views etc.
        for col in df.iloc[:, 2:-1].columns:
            df[col] = df[col].astype(int)

        connection = json.loads(os.environ.get('DB_TEST_CONNECTION'))
        query = '''
           create table if not exists {db}.tereshin_metrics
           (
           event_date Date,
           dimension String,
           dimension_value String,
           views Int64,
           likes Int64,
           messages_received Int64,
           messages_sent Int64,
           users_received Int64,
           users_sent Int64
           )
           engine = MergeTree()
           order by event_date
           '''
        ph.execute(query, connection=connection)
        ph.to_clickhouse(df, 'tereshin_metrics', connection=connection, index=False)

    df_feed = extract_feed()
    df_messenger = extract_messenger()
    df_joined = merge_extracted_tables(df_feed, df_messenger)
    df_os = os_aggregation(df_joined)
    df_gender = gender_aggregation(df_joined)
    df_age = age_aggregation(df_joined)
    load(df_os, df_gender, df_age)


atereshin_dag = atereshin_dag()
