import json
import os
import io
from datetime import date, timedelta, datetime

import seaborn as sns
import pandahouse as ph
import matplotlib.pyplot as plt
import telegram
from airflow.decorators import dag, task

load_dotenv()

def extract_df(query):
    connection = json.loads(os.environ.get('DB_CONNECTION'))
    df = ph.read_clickhouse(query, connection=connection)
    return df


default_args = {
    'owner': 'a-tereshin',
    'depends_on_past': False,
    'retries': 2,
    'retry_delay': timedelta(seconds=300),
    'start_date': datetime(2022, 2, 14)
}
schedule_interval = '58 10 * * *'  # cron-expression (daily at 10:58 am MSK)


@dag(default_args=default_args, schedule_interval=schedule_interval, catchup=False)
def atereshin_bot_app_dag():
    @task()
    def extract_dau():
        query_dau = """
            select date, count(distinct user_id) dau, 'messenger and newsfeed' app
            from 
            (select distinct toDate(time) date, user_id from simulator_20230120.feed_actions 
            where date between (today() - 7) and yesterday()) a
            join
            (select distinct toDate(time) date, user_id from simulator_20230120.message_actions 
            where date between (today() - 7) and yesterday()) b 
            using user_id
            group by date
            union all
            select toDate(time) date, count(distinct user_id), 'only newsfeed' app
            from simulator_20230120.feed_actions
            where user_id not in (select distinct user_id from simulator_20230120.message_actions) 
            and date between (today() - 7) and yesterday()
            group by date
            union all
            select *, 'only messenger' app from 
            (select toDate(time) date,count(distinct user_id)
            from simulator_20230120.message_actions
            where user_id not in (select distinct user_id from simulator_20230120.feed_actions) and date between (today() - 7) and yesterday()
            group by date) a
            full outer join 
            (select distinct toDate(time) date
            from simulator_20230120.message_actions
            where date between (today() - 7) and yesterday()) b
            using date
            """
        df_dau = extract_df(query_dau)
        return df_dau

    @task()
    def extract_peak_time():
        query_peak_time = """
            select day, hour, count(user_id) actions
            from
            (select user_id, toDate(time) as day, toHour(time) as hour 
            from simulator_20230120.feed_actions 
            where day between (today() - 7) and yesterday()
            union all 
            select user_id, toDate(time) as day, toHour(time) as hour 
            from simulator_20230120.message_actions 
            where day between (today() - 7) and yesterday()) a
            group by day, hour
            """
        df_peak_time = extract_df(query_peak_time)
        return df_peak_time

    @task()
    def extract_retention():
        query_retention = """
            select action, start, source, count(user_id) users
            from
            (select *, min(action) over (partition by user_id) start
            from
            (select distinct user_id, toDate(time) action, source
            from simulator_20230120.message_actions
            union distinct 
            select distinct user_id, toDate(time) action, source
            from simulator_20230120.feed_actions))
            group by action, start, source
            having start >= (today() - 7)
            """
        df_retention = extract_df(query_retention)
        return df_retention

    @task()
    def extract_actions():
        query_actions = """
            select a.date date, a.messages messages, b.likes likes, b.views views, c.posts posts
            from
            (select toDate(time) date, count(user_id) messages
            from simulator_20230120.message_actions
            group by date) a
            full outer join
            (select toDate(time) date, countIf(action = 'like') likes, countIf(action = 'view') views
            from simulator_20230120.feed_actions
            group by date) b
            on a.date = b.date
            full outer join
            (select date, count(post_id) posts
            from 
            (select post_id, min(toDate(time)) date
            from simulator_20230120.feed_actions
            group by post_id)
            group by date) c
            on b.date = c.date
            where date != today()
            """
        df_actions = extract_df(query_actions)
        return df_actions

    @task()
    def extract_acquisition():
        query_acquisition = """
            select start, source, count(user_id) new_users
            from
            (select distinct user_id, min(action) over (partition by user_id) start, source
            from
            (select distinct user_id, toDate(time) action, source
            from simulator_20230120.message_actions
            union distinct 
            select distinct user_id, toDate(time) action, source
            from simulator_20230120.feed_actions))
            group by start, source
            having start != today()
            """
        df_acquisition = extract_df(query_acquisition)
        return df_acquisition

    @task()
    def send_message(df_acquisition, df_dau, df_actions):
        chat_id = json.loads(os.environ.get('CHAT_ID))
        token = json.loads(os.environ.get('TOKEN))
        bot = telegram.Bot(token=token)

        message = f"Ежедневный отчет по приложению.\n\n" \
                  f"На *{(date.today() - timedelta(days=1)).strftime('%d/%m/%Y')}*:\n\n" \
                  f"*Аудитория приложения* : {df_acquisition.new_users.sum()}\n" \
                  f"*Новых ads пользователей* : {df_acquisition.new_users[('ads' == df_acquisition.source) & (df_acquisition.start == df_acquisition.start.max())].item()}\n" \
                  f"*Новых organic пользователей* : {df_acquisition.new_users[('organic' == df_acquisition.source) & (df_acquisition.start == df_acquisition.start.max())].item()}\n" \
                  f"*DAU приложения* : {df_dau.dau[df_dau.date == df_dau.date.max()].sum()}\n" \
                  f"*DAU и лента и мессенджер* : {df_dau.dau[(df_dau.app == 'messenger and newsfeed') & (df_dau.date == df_dau.date.max())].item()}\n" \
                  f"*DAU только ленты новостей* : {df_dau.dau[(df_dau.app == 'only newsfeed') & (df_dau.date == df_dau.date.max())].item()}\n" \
                  f"*DAU только мессенджера* : {df_dau.dau[(df_dau.app == 'only messenger') & (df_dau.date == df_dau.date.max())].item()}\n\n" \
                  f"*Всего постов в ленте* : {df_actions.posts.sum()}\n" \
                  f"*Новых постов в ленте* : {df_actions.posts[df_actions.date == df_actions.date.max()].item()}\n" \
                  f"*Просмотры* : {df_actions.views[df_actions.date == df_actions.date.max()].item()}\n" \
                  f"*Лайки* : {df_actions.likes[df_actions.date == df_actions.date.max()].item()}\n" \
                  f"*Сообщения* : {df_actions.messages[df_actions.date == df_actions.date.max()].item()}\n" \

        bot.sendMessage(chat_id=chat_id, text=message, parse_mode=telegram.ParseMode.MARKDOWN)

    @task()
    def send_plots(df_actions, df_retention, df_peak_time):
        df_action_week = df_actions[df_actions.date > datetime.today() - timedelta(days=8)]
        plt_date = df_action_week.date.dt.strftime('%d %b')
        plt.figure(figsize=(40, 43))
        plt.rcParams.update({'font.size': 21})
        plt.suptitle(f'App Actions\n{(date.today() - timedelta(days=1)).strftime("%d/%m/%Y")}', x=0.513, y=0.92, fontsize=27)

        plt.subplot(311)
        plt.plot(plt_date, df_action_week.views, marker='o', markerfacecolor='w')
        plt.plot(plt_date, df_action_week.likes, marker='o', markerfacecolor='w')
        plt.legend(['views', 'likes'])
        plt.title('Views and Likes', pad=20)
        curr_values = plt.gca().get_yticks()
        plt.gca().set_yticklabels(['{:,.0f}'.format(x) for x in curr_values])
        plt.grid(True)

        plt.subplot(313)
        plt.plot(plt_date, df_action_week.posts, color='darkcyan', marker='o', markerfacecolor='w')
        plt.title('Posts', pad=20)
        plt.grid(True)

        plt.subplot(312)
        plt.plot(plt_date, df_action_week.messages, color='darkcyan', marker='o', markerfacecolor='w')
        plt.title('Messages', pad=20)
        plt.grid(True)

        plot_action = io.BytesIO()
        plt.savefig(plot_action)
        plot_action.name = f"app_actions{(date.today() - timedelta(days=1)).strftime('%d/%m/%Y')}.png"
        plot_action.seek(0)
        plt.close()

        out = df_peak_time.groupby(['hour', 'day'])['actions'].max().unstack().sort_index(axis=0, ascending=False)
        out.columns = list(map(lambda x: datetime.strftime(x, format='%a %d %b'), out.columns))
        plt.figure(figsize=(20, 15))
        plt.title("Тепловая карта активности за последние 7 дней", y=1.03)
        sns.heatmap(out)

        activity_heatmap = io.BytesIO()
        plt.savefig(activity_heatmap)
        activity_heatmap.name = f"app_activity_heatmap{(date.today() - timedelta(days=1)).strftime('%d/%m/%Y')}.png"
        activity_heatmap.seek(0)
        plt.close()

        df_rr_organic = df_retention[df_retention.source == 'organic']

        organic = df_rr_organic.groupby(['start', 'action'])['users'].max().unstack()
        organic.columns = list(map(lambda x: datetime.strftime(x, format='%a %d %b'), organic.columns))
        organic.index = list(map(lambda x: datetime.strftime(x, format='%a %d %b'), organic.index))

        df_rr_ads = df_retention[df_retention.source == 'ads']

        ads = df_rr_ads.groupby(['start', 'action'])['users'].max().unstack()
        ads.columns = list(map(lambda x: datetime.strftime(x, format='%a %d %b'), ads.columns))
        ads.index = list(map(lambda x: datetime.strftime(x, format='%a %d %b'), ads.index))

        plt.figure(figsize=(50, 18))
        plt.suptitle(f'Retention Rate по каналам трафика за последние 7 дней\n{(date.today() - timedelta(days=1)).strftime("%d/%m/%Y")}')

        plt.subplot(121)
        sns.heatmap(organic, annot=True, fmt='g', square=True, linewidths=.5, cmap=sns.cubehelix_palette(8))
        plt.yticks(rotation=0)
        plt.xticks(rotation=45)

        plt.subplot(122)
        sns.heatmap(ads, annot=True, fmt='g', square=True, linewidths=.5, cmap=sns.cubehelix_palette(8))
        plt.yticks(rotation=0)
        plt.xticks(rotation=45)

        rr_heatmap = io.BytesIO()
        plt.savefig(rr_heatmap)
        rr_heatmap.name = f"rr_heatmap{(date.today() - timedelta(days=1)).strftime('%d/%m/%Y')}.png"
        rr_heatmap.seek(0)
        plt.close()

        chat_id = json.loads(os.environ.get('CHAT_ID))
        token = json.loads(os.environ.get('TOKEN))
        bot = telegram.Bot(token=token)

        bot.sendPhoto(chat_id=chat_id, photo=plot_action,
                      caption="График с лайками, просмотрами, сообщениями и постами за предыдущие 7 дней")
        bot.sendPhoto(chat_id=chat_id, photo=activity_heatmap,
                      caption="Тепловая карта активности в приложении по часам/дням за предыдущие 7 дней")
        bot.sendPhoto(chat_id=chat_id, photo=rr_heatmap,
                      caption="Retention Rate по каналам трафика за предыдущие 7 дней")


    df_acquisition = extract_acquisition()
    df_dau = extract_dau()
    df_actions = extract_actions()
    send_message(df_acquisition, df_dau, df_actions)
    df_retention = extract_retention()
    df_peak_time = extract_peak_time()
    send_plots(df_actions, df_retention, df_peak_time)


atereshin_bot_app_dag = atereshin_bot_app_dag()
