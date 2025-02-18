import sqlite3
import pandas as pd
import numpy as np
from sklearn.cluster import KMeans
import json

# Путь к базе данных
DB_PATH = "/Users/sasha/IdeaProjects/nomorejobfuckery/data/devops_jobs.db"
SQL_FILE_PATH = "/Users/sasha/IdeaProjects/nomorejobfuckery/jobfuq/sql/filtered_jobs.sql"

# Загружаем данные из SQLite в DataFrame
def load_data():
    conn = sqlite3.connect(DB_PATH)
    query = "SELECT * FROM job_listings WHERE is_posted = 1;"
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df

# Анализ данных: вычисление статистик и распределений
def analyze_data(df):
    stats = {
        'skills_match': {'mean': df['skills_match'].mean(), 'median': df['skills_match'].median()},
        'confidence': {'mean': df['confidence'].mean(), 'median': df['confidence'].median()},
        'success_probability': {'mean': df['success_probability'].mean(), 'median': df['success_probability'].median()},
        'effort_days_to_fit': {'mean': df['effort_days_to_fit'].mean(), 'median': df['effort_days_to_fit'].median()},
        'critical_skill_mismatch_penalty': {'mean': df['critical_skill_mismatch_penalty'].mean(), 'median': df['critical_skill_mismatch_penalty'].median()}
    }
    return stats

# Кластеризация вакансий на основе ключевых параметров
def cluster_jobs(df, n_clusters=3):
    features = df[['skills_match', 'confidence', 'success_probability', 'effort_days_to_fit', 'critical_skill_mismatch_penalty']]
    features = features.dropna()
    kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    df.loc[features.index, 'cluster'] = kmeans.fit_predict(features)
    return df

# Обновление SQL-запроса с новыми динамическими порогами
def update_sql_query(stats):
    with open(SQL_FILE_PATH, 'r', encoding='utf-8') as file:
        sql_query = file.read()

    # Динамическое обновление порогов (пример)
    new_query = sql_query.replace("skills_match >= 0.70", f"skills_match >= {stats['skills_match']['median']:.2f}")
    new_query = new_query.replace("confidence >= 0.70", f"confidence >= {stats['confidence']['median']:.2f}")
    new_query = new_query.replace("success_probability >= 0.65", f"success_probability >= {stats['success_probability']['median']:.2f}")
    new_query = new_query.replace("effort_days_to_fit <= 12", f"effort_days_to_fit <= {int(stats['effort_days_to_fit']['median'])}")

    with open(SQL_FILE_PATH, 'w', encoding='utf-8') as file:
        file.write(new_query)
    print("SQL-запрос обновлен с новыми динамическими порогами.")

# Основная функция анализа и адаптации SQL-запроса
def main():
    df = load_data()
    stats = analyze_data(df)
    df = cluster_jobs(df)
    update_sql_query(stats)

    # # Сохранение кластеризованных данных для анализа
    # df.to_csv("../analytics/clustered_jobs.csv", index=False)
    # print("Кластеризованные данные сохранены в clustered_jobs.csv")

if __name__ == "__main__":
    main()